from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton,
    QLineEdit, QHBoxLayout, QTableView, QFormLayout, QCheckBox,
    QAbstractItemView, QMessageBox, QFileDialog, QHeaderView,
    QMenu, QAction, QComboBox,QTableWidget, QTableWidgetItem, QDialog, QInputDialog
)
from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QPoint
from PyQt5.QtGui import QBrush, QColor, QGuiApplication, QKeySequence, QPixmap

import math
import datetime
import numpy as np
import pandas as pd
import json
import os
import re

from utils import get_df_from_db, to_jst_naive, translate_dict

# --------------------------------------------------------------------------------------
# 共通定数 / ヘルパ
# --------------------------------------------------------------------------------------

SETTINGS_PATH = os.path.join(os.getcwd(), "settings.json")
KPI_INTERVALS_PATH = os.path.join(os.getcwd(), "kpi.json")

TRACK_ORDER = [
    "FP",
    "SB1",
    "0m",
    "60m",
    "AP1",
    "50m",
    "100m",
    "BP",
    "150m",
    "AP2",
    "200m",
]
# 名前系は常に先頭に出したい列
NAME_COLUMNS = ["first_name", "last_name", "Date", "FP"]


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(path: str, obj: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _validate_position_names(config: dict) -> list[str]:
    """
    kpi.jsonの地点名を検証し、無効な地点名のリストを返す
    
    Returns:
        無効な地点名のリスト（例: ["125m", "invalid"]）
    """
    invalid_positions = []
    
    def extract_position_name(pos_str: str) -> str:
        """地点名から位置名を抽出（オフセットを除去）"""
        if not pos_str:
            return ""
        # "0m+1" -> "0m", "FP" -> "FP"
        match = re.match(r'^(.+?)(\+(\d+))?$', pos_str)
        if match:
            return match.group(1)
        return pos_str
    
    # "version"と"settings"以外のキーをモードとして扱う
    for key in config.keys():
        if key == "version" or key == "settings":
            continue
        
        entries = config.get(key, [])
        if not isinstance(entries, list):
            continue
        
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            
            start = ent.get("start", "")
            end = ent.get("end", "")
            
            if start:
                start_pos = extract_position_name(start)
                if start_pos and start_pos not in TRACK_ORDER:
                    if start_pos not in invalid_positions:
                        invalid_positions.append(start_pos)
            
            if end:
                end_pos = extract_position_name(end)
                if end_pos and end_pos not in TRACK_ORDER:
                    if end_pos not in invalid_positions:
                        invalid_positions.append(end_pos)
    
    return invalid_positions


# -------------------------------------------------------------------------------------- 
# カスタムQTableWidgetItem（空欄を最後にソート）
# --------------------------------------------------------------------------------------

class NumericTableWidgetItem(QTableWidgetItem):
    """
    数値用のQTableWidgetItem。
    空欄を常に最後に来るようにソートする。
    """
    def __lt__(self, other):
        """
        ソート時の比較メソッド。
        空欄（text()が空またはNone）は常に最後に来るようにする。
        """
        # 自分が空欄かどうかをチェック
        self_text = self.text() or ""
        self_data = self.data(Qt.EditRole)
        
        # 空欄の判定: text()が空文字列、またはdata(Qt.EditRole)がNone
        # 注意: data(Qt.EditRole)がNoneの場合、空欄とみなす
        self_is_empty = False
        if self_text.strip() == "":
            # text()が空の場合
            if self_data is None:
                # data(Qt.EditRole)もNoneなら確実に空欄
                self_is_empty = True
            elif isinstance(self_data, float) and math.isnan(self_data):
                # NaNの場合も空欄
                self_is_empty = True
        
        # 相手が空欄かどうかをチェック
        other_text = other.text() or ""
        other_data = other.data(Qt.EditRole)
        
        other_is_empty = False
        if other_text.strip() == "":
            if other_data is None:
                other_is_empty = True
            elif isinstance(other_data, float) and math.isnan(other_data):
                other_is_empty = True
        
        # 自分が空欄の場合
        if self_is_empty:
            return False  # 空欄は常に後ろ（昇順では最後に来る）
        
        # 相手が空欄の場合
        if other_is_empty:
            return True  # 相手が空欄なら自分が前
        
        # 両方とも数値として比較（data(Qt.EditRole)を優先）
        try:
            if self_data is not None and other_data is not None:
                # EditRoleのデータが数値の場合
                if isinstance(self_data, (int, float)) and isinstance(other_data, (int, float)):
                    if math.isnan(self_data) or math.isnan(other_data):
                        # NaNの場合は空欄扱い
                        if math.isnan(self_data):
                            return False
                        return True
                    return float(self_data) < float(other_data)
            
            # text()から数値を取得して比較
            if self_text.strip() and other_text.strip():
                self_val = float(self_text)
                other_val = float(other_text)
                return self_val < other_val
        except (ValueError, TypeError):
            pass
        
        # 数値として比較できない場合は文字列として比較
        return self_text < other_text

# -------------------------------------------------------------------------------------- 
# DataFrame -> Qt Model
# --------------------------------------------------------------------------------------

class DataFrameModel(QAbstractTableModel):
    """
    単純な DataFrame 表示モデル。
    - Timestamp: HH:MM:SS(.mmm)
    - Timedelta: MM:SS.mmm
    - それ以外: str(val)
    """

    def __init__(self, df: pd.DataFrame, mask: pd.DataFrame | None = None):
        super().__init__()
        self._df = df.reset_index(drop=True)
        self._mask = mask

    def rowCount(self, parent=QModelIndex()):
        return len(self._df)

    def columnCount(self, parent=QModelIndex()):
        return len(self._df.columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        val = self._df.iat[index.row(), index.column()]

        if role == Qt.DisplayRole:
            if pd.isna(val):
                return ""

            # Timestamp 系
            if isinstance(val, (pd.Timestamp, datetime.datetime)):
                ms = val.microsecond // 1000
                fmt = "%H:%M:%S" if ms == 0 else "%H:%M:%S.%f"
                text = val.strftime(fmt)
                return text if ms == 0 else text[:-3]

            # Timedelta 系
            if isinstance(val, pd.Timedelta):
                total_ms = int(val / pd.Timedelta(milliseconds=1))
                sign = "-" if total_ms < 0 else ""
                total_ms = abs(total_ms)
                minutes, rem = divmod(total_ms, 60_000)
                seconds, ms = divmod(rem, 1000)
                return f"{sign}{minutes:02d}:{seconds:02d}.{ms:03d}"

            return str(val)

        # 補完セルなら赤字
        if role == Qt.ForegroundRole and self._mask is not None:
            try:
                if bool(self._mask.iat[index.row(), index.column()]):
                    return QBrush(QColor(220, 0, 0))
            except Exception:
                pass

        if role == Qt.EditRole:
            return "" if pd.isna(val) else val

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._df.columns[section]
        return str(section + 1)


class RangeFilterProxy(QSortFilterProxyModel):
    """
    単一KPIに対する数値レンジフィルタ。
    - visible_columns: 表示されている列名の順序
    - filter_col: 絞り込み対象列名（None ならフィルタ無し）
    - vmin, vmax: 数値レンジ（両方 None ならフィルタ無し）
    """

    def __init__(self, visible_columns: list[str]):
        super().__init__()
        self.visible_columns = list(visible_columns)
        self.filter_col: str | None = None
        self.vmin: float | None = None
        self.vmax: float | None = None

    def set_visible_columns(self, visible_columns: list[str]):
        self.visible_columns = list(visible_columns)
        self.invalidateFilter()

    def set_filter_column(self, col_name: str | None):
        self.filter_col = col_name
        self.invalidateFilter()

    def set_range(self, vmin: float | None, vmax: float | None):
        self.vmin = vmin
        self.vmax = vmax
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()

        # フィルタ列が指定されていない／レンジが無い場合は全通し
        if not self.filter_col or (self.vmin is None and self.vmax is None):
            return True

        try:
            col_idx = self.visible_columns.index(self.filter_col)
        except ValueError:
            return True  # 表示されていなければフィルタしない

        idx = model.index(source_row, col_idx)
        text = model.data(idx, Qt.DisplayRole)
        if text in (None, ""):
            return False

        try:
            val = float(text)
        except Exception:
            return False

        if self.vmin is not None and val < self.vmin:
            return False
        if self.vmax is not None and val > self.vmax:
            return False

        return True


class KPIJsonEditorPage(QWidget):
    """
    kpi.json をGUIで編集するページ。

    上: トラック図の画像
    下: モード(rolling/standing/flying)ごとの start/end 区間の一覧と追加・削除UI
    """

    def __init__(self, kpi_page: "KPIPage"):
        super().__init__()
        self.kpi_page = kpi_page
        self.stacked_widget = kpi_page.stacked_widget

        # 元の設定をコピーして編集用に保持
        import copy
        self._config = copy.deepcopy(kpi_page._interval_config) or {}
        # 利用可能なモードを取得（kpi_pageから）
        self._available_modes = kpi_page._get_available_modes() if hasattr(kpi_page, '_get_available_modes') else []
        # 各モードが存在することを確認（存在しない場合は空リストを設定）
        for mode in self._available_modes:
            if mode not in self._config:
                self._config[mode] = []

        # start/end で選べる地点（TRACK_ORDERをそのまま使用）
        self.available_points = list(TRACK_ORDER)

        self._build_ui()
        self._refresh_mode_entries()

    # ------------------------------------------------------------------
    # UI 構築
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("Edit KPI")
        main = QVBoxLayout()

        # 画像エリア
        img_label = QLabel()
        path = getattr(self.kpi_page, "track_image_path", "")
        if path and os.path.exists(path):
            pix = QPixmap(path)
            if not pix.isNull():
                pix = pix.scaledToWidth(900, Qt.SmoothTransformation)
                img_label.setPixmap(pix)
                img_label.setAlignment(Qt.AlignCenter)
            else:
                img_label.setText(f"Cannot load image: {path}")
        else:
            img_label.setText(f"Track image not found: {path}")
        main.addWidget(img_label)

        # モード選択
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        # 利用可能なモードをドロップダウンに追加
        for mode in self._available_modes:
            # モード名を表示用に整形（最初の文字を大文字に）
            display_name = mode.capitalize()
            self.mode_combo.addItem(display_name, userData=mode)
        self.mode_combo.currentIndexChanged.connect(self._refresh_mode_entries)
        mode_row.addWidget(self.mode_combo)
        
        # 新規モード追加ボタン
        self.btnAddMode = QPushButton("Add New Mode")
        self.btnAddMode.clicked.connect(self._on_add_mode_clicked)
        mode_row.addWidget(self.btnAddMode)
        
        mode_row.addStretch()
        main.addLayout(mode_row)

        # 現在の定義一覧テーブル
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Display Name", "start", "end"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        main.addWidget(self.table)

        # 追加用UI
        add_row = QHBoxLayout()
        self.start_combo = QComboBox()
        self.start_combo.addItems(self.available_points)
        self.end_combo = QComboBox()
        self.end_combo.addItems(self.available_points)
        
        # startのオフセット用ドロップダウン（次の周回、次の次の周回など）
        self.start_offset_combo = QComboBox()
        for i in range(11):  # 0から10まで
            self.start_offset_combo.addItem(str(i), userData=i)
        
        # endのオフセット用ドロップダウン（次の周回、次の次の周回など）
        self.end_offset_combo = QComboBox()
        for i in range(11):  # 0から10まで
            self.end_offset_combo.addItem(str(i), userData=i)
        
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Display Name（start-end if empty）")

        add_row.addWidget(QLabel("start:"))
        add_row.addWidget(self.start_combo)
        add_row.addWidget(QLabel("start offset:"))
        add_row.addWidget(self.start_offset_combo)
        add_row.addWidget(QLabel("end:"))
        add_row.addWidget(self.end_combo)
        add_row.addWidget(QLabel("end offset:"))
        add_row.addWidget(self.end_offset_combo)
        add_row.addWidget(QLabel("Display Name:"))
        add_row.addWidget(self.name_edit)

        self.btnAdd = QPushButton("Add")
        self.btnAdd.clicked.connect(self._on_add_clicked)
        add_row.addWidget(self.btnAdd)

        main.addLayout(add_row)
        
        # Usage comment
        comment_label = QLabel("Note: offset specifies which lap to use. Example: start=0m, offset=1 → next lap's 0m")
        comment_label.setWordWrap(True)
        comment_label.setStyleSheet("color: gray; font-size: 10pt;")
        main.addWidget(comment_label)

        # 操作用ボタン
        btn_row = QHBoxLayout()
        self.btnDelete = QPushButton("Delete Selected Row")
        self.btnDelete.clicked.connect(self._on_delete_clicked)
        btn_row.addWidget(self.btnDelete)

        btn_row.addStretch()

        self.btnCancel = QPushButton("Cancel")
        self.btnCancel.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self.btnCancel)

        self.btnSave = QPushButton("Save and Back")
        self.btnSave.clicked.connect(self._on_save_clicked)
        btn_row.addWidget(self.btnSave)

        main.addLayout(btn_row)

        self.setLayout(main)

    # ------------------------------------------------------------------
    # モードごとの一覧表示
    # ------------------------------------------------------------------
    def _current_mode(self) -> str:
        data = self.mode_combo.currentData()
        if data:
            return data
        # デフォルトは最初のモード
        return self._available_modes[0] if self._available_modes else ""

    def _refresh_mode_entries(self):
        mode = self._current_mode()
        entries = self._config.get(mode, [])

        self.table.setRowCount(0)
        for ent in entries:
            start = ent.get("start", "")
            end = ent.get("end", "")
            name = ent.get("name") or f"{start}-{end}"
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.table.setItem(row, 1, QTableWidgetItem(str(start)))
            self.table.setItem(row, 2, QTableWidgetItem(str(end)))

    # ------------------------------------------------------------------
    # 追加 / 削除 / 保存 / 戻る
    # ------------------------------------------------------------------
    def _on_add_mode_clicked(self):
        """新規モードを追加"""
        # モード名入力ダイアログ
        mode_name, ok = QInputDialog.getText(
            self,
            "Add New Mode",
            "Enter mode name:"
        )
        
        if not ok or not mode_name:
            return
        
        mode_name = mode_name.strip()
        if not mode_name:
            QMessageBox.warning(self, "Invalid Name", "Mode name cannot be empty.")
            return
        
        # 既に存在するモード名かチェック
        if mode_name in self._available_modes:
            QMessageBox.warning(self, "Mode Exists", f"Mode '{mode_name}' already exists.")
            return
        
        # 予約語チェック
        if mode_name.lower() in ("version", "settings"):
            QMessageBox.warning(self, "Invalid Name", f"'{mode_name}' is a reserved keyword.")
            return
        
        # 新しいモードを追加
        self._config[mode_name] = []
        self._available_modes.append(mode_name)
        self._available_modes.sort()  # ソート
        
        # ドロップダウンを更新
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for mode in self._available_modes:
            display_name = mode.capitalize()
            self.mode_combo.addItem(display_name, userData=mode)
        self.mode_combo.blockSignals(False)
        
        # 新規追加したモードを選択
        new_index = self._available_modes.index(mode_name)
        self.mode_combo.setCurrentIndex(new_index)
        
        # エントリをリフレッシュ（空のリストが表示される）
        self._refresh_mode_entries()
    
    def _on_add_clicked(self):
        mode = self._current_mode()
        start_base = self.start_combo.currentText()
        start_offset = self.start_offset_combo.currentData()
        end_base = self.end_combo.currentText()
        end_offset = self.end_offset_combo.currentData()
        name = self.name_edit.text().strip()

        if not start_base or not end_base:
            QMessageBox.warning(self, "Cannot add.", "Please select both start and end.")
            return

        # startにオフセットを追加（+0の場合は省略）
        if start_offset and start_offset > 0:
            start = f"{start_base}+{start_offset}"
        else:
            start = start_base

        # endにオフセットを追加（+0の場合は省略）
        if end_offset and end_offset > 0:
            end = f"{end_base}+{end_offset}"
        else:
            end = end_base

        entry = {"start": start, "end": end}
        if name:
            entry["name"] = name

        self._config.setdefault(mode, []).append(entry)
        self.name_edit.clear()
        self.start_offset_combo.setCurrentIndex(0)  # オフセットをリセット
        self.end_offset_combo.setCurrentIndex(0)  # オフセットをリセット
        self._refresh_mode_entries()

    def _on_delete_clicked(self):
        mode = self._current_mode()
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return

        rows = sorted(r.row() for r in sel)
        rows.reverse()  # 下から消す
        entries = self._config.get(mode, [])
        for r in rows:
            if 0 <= r < len(entries):
                entries.pop(r)
        self._refresh_mode_entries()

    def _on_cancel_clicked(self):
        # 何も保存せずに元の画面へ戻る
        sw = self.stacked_widget
        sw.setCurrentWidget(self.kpi_page)
        sw.removeWidget(self)
        self.deleteLater()

    def _on_save_clicked(self):
        # 地点名の検証
        invalid_positions = _validate_position_names(self._config)
        if invalid_positions:
            valid_positions = ", ".join(TRACK_ORDER)
            reply = QMessageBox.warning(
                self,
                "Invalid Position Names",
                f"Invalid position names found:\n\n{', '.join(invalid_positions)}\n\n"
                f"Valid position names are:\n{valid_positions}\n\n"
                f"Do you want to save anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        # JSONファイルに保存
        try:
            _save_json(KPI_INTERVALS_PATH, self._config)
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Failed to save kpi.json:\n{e}")
            return

        # 親ページに反映
        self.kpi_page._interval_config = self._config
        # 利用可能なモードを更新
        if hasattr(self.kpi_page, '_get_available_modes'):
            self.kpi_page._available_modes = self.kpi_page._get_available_modes()
            # モード選択ドロップダウンを更新
            if hasattr(self.kpi_page, 'mode_combo'):
                current_mode = self.kpi_page.time_mode
                self.kpi_page.mode_combo.blockSignals(True)
                self.kpi_page.mode_combo.clear()
                for mode in self.kpi_page._available_modes:
                    display_name = mode.capitalize()
                    self.kpi_page.mode_combo.addItem(display_name, userData=mode)
                # 現在のモードを選択（存在しない場合は最初のモード）
                if current_mode in self.kpi_page._available_modes:
                    index = self.kpi_page._available_modes.index(current_mode)
                    self.kpi_page.mode_combo.setCurrentIndex(index)
                else:
                    self.kpi_page.mode_combo.setCurrentIndex(0)
                    if self.kpi_page._available_modes:
                        self.kpi_page.time_mode = self.kpi_page._available_modes[0]
                self.kpi_page.mode_combo.blockSignals(False)
        # KPI計算はエフォートごとに行うため、ここでは不要

        QMessageBox.information(self, "Saved Successfully", "kpi.json has been saved.")

        sw = self.stacked_widget
        sw.setCurrentWidget(self.kpi_page)
        sw.removeWidget(self)
        self.deleteLater()


# --------------------------------------------------------------------------------------
# KPI Page 本体
# --------------------------------------------------------------------------------------

class EffortRawDataPage(QDialog):
    """
    エフォートの生データを表示するダイアログ（別ウィンドウ）
    """
    
    def __init__(self, kpi_page: "KPIPage", effort_data: dict):
        super().__init__(kpi_page)
        self.kpi_page = kpi_page
        self.effort_data = effort_data
        
        # モーダルレス（非モーダル）で開くように設定
        self.setModal(False)
        # 閉じられたときに自動的に削除されるように設定
        self.setAttribute(Qt.WA_DeleteOnClose)
        # 常に前面に来ないようにウィンドウフラグを設定
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        
        self._build_ui()
    
    def _build_ui(self):
        self.setWindowTitle("Effort Raw Data")
        self.setMinimumSize(1000, 600)
        main_layout = QVBoxLayout()
        
        # タイトル
        title = QLabel(f"Effort Raw Data - {self.effort_data.get('player_name', 'Unknown')}")
        title.setStyleSheet("font-size: 14pt; font-weight: bold;")
        main_layout.addWidget(title)
        
        # エフォート情報
        info_layout = QHBoxLayout()
        info_layout.addWidget(QLabel(f"Start: {self.effort_data.get('start_time')}"))
        info_layout.addWidget(QLabel(f"Date: {self.effort_data.get('date')}"))
        info_layout.addStretch()
        main_layout.addLayout(info_layout)
        
        # テーブル
        self.table = QTableView()
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setAlternatingRowColors(True)
        main_layout.addWidget(self.table)
        
        # ボタン
        button_layout = QHBoxLayout()
        
        # CSV保存ボタン
        save_csv_btn = QPushButton("Save as CSV")
        save_csv_btn.clicked.connect(self._save_to_csv)
        button_layout.addWidget(save_csv_btn)
        
        button_layout.addStretch()
        
        # 閉じるボタン
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        button_layout.addWidget(close_btn)
        
        main_layout.addLayout(button_layout)
        
        self.setLayout(main_layout)
        
        # データを表示
        self._load_data()
    
    def _load_data(self):
        """エフォートの生データをテーブルに表示（lap番号順、TRACK_ORDERを列名としてtimestampを格納）"""
        if not self.effort_data.get("data_points"):
            self.model = DataFrameModel(pd.DataFrame())
            self.table.setModel(self.model)
            return
        
        # data_pointsをDataFrameに変換
        data_points = self.effort_data["data_points"]
        df = pd.DataFrame(data_points)
        
        if df.empty:
            self.model = DataFrameModel(pd.DataFrame())
            self.table.setModel(self.model)
            return
        
        # timestamp順にソート
        if "timestamp" not in df.columns or "position" not in df.columns:
            self.model = DataFrameModel(pd.DataFrame())
            self.table.setModel(self.model)
            return
        
        df = df.sort_values("timestamp").reset_index(drop=True)
        
        # lap_number列があるか確認
        if "lap_number" not in df.columns:
            # lap_numberがない場合は従来の方法（0mの出現回数で周回を識別）
            zero_m_rows = df[df["position"] == "0m"].copy()
            
            if zero_m_rows.empty:
                # 0mがない場合は、全データを1周回として扱う
                rows_data = []
                row_dict = {}
                for pos in TRACK_ORDER:
                    pos_rows = df[df["position"] == pos]
                    if not pos_rows.empty:
                        row_dict[pos] = pos_rows.iloc[0]["timestamp"]
                    else:
                        row_dict[pos] = None
                rows_data.append(row_dict)
                df_view = pd.DataFrame(rows_data)
            else:
                # 各0mの間を1周回として扱う
                rows_data = []
                
                for i in range(len(zero_m_rows)):
                    # この周回の開始時刻（前の0m、またはエフォート開始時刻）
                    if i == 0:
                        lap_start_time = df.iloc[0]["timestamp"]
                    else:
                        lap_start_time = zero_m_rows.iloc[i-1]["timestamp"]
                    
                    # この周回の終了時刻（この0m）
                    lap_end_time = zero_m_rows.iloc[i]["timestamp"]
                    
                    # この周回のデータを取得
                    lap_df = df[
                        (df["timestamp"] >= lap_start_time) & 
                        (df["timestamp"] <= lap_end_time)
                    ].copy()
                    
                    # 各位置のtimestampを抽出
                    row_dict = {}
                    for pos in TRACK_ORDER:
                        pos_rows = lap_df[lap_df["position"] == pos]
                        if not pos_rows.empty:
                            # 最初のtimestampを使用
                            row_dict[pos] = pos_rows.iloc[0]["timestamp"]
                        else:
                            row_dict[pos] = None
                    
                    rows_data.append(row_dict)
                
                df_view = pd.DataFrame(rows_data)
        else:
            # lap_numberに基づいてlapごとに1行を作成
            rows_data = []
            
            # lap番号でグループ化（lap番号順にソート）
            unique_laps = sorted(df["lap_number"].unique())
            
            for lap_num in unique_laps:
                # このlapのデータを取得
                lap_df = df[df["lap_number"] == lap_num].copy()
                
                # 各位置のtimestampを抽出
                row_dict = {}
                for pos in TRACK_ORDER:
                    pos_rows = lap_df[lap_df["position"] == pos]
                    if not pos_rows.empty:
                        # 最初のtimestampを使用
                        row_dict[pos] = pos_rows.iloc[0]["timestamp"]
                    else:
                        row_dict[pos] = None
                
                rows_data.append(row_dict)
            
            df_view = pd.DataFrame(rows_data)
        
        # 列の順序をTRACK_ORDERに合わせる
        df_view = df_view[TRACK_ORDER]
        
        self.model = DataFrameModel(df_view)
        self.table.setModel(self.model)
        
        # 見た目調整
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        
        default_width = 160
        visible_cols = list(self.model._df.columns)
        for i, c in enumerate(visible_cols):
            self.table.setColumnWidth(i, default_width)
        self.table.verticalHeader().setDefaultSectionSize(26)
        
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
    
    def _save_to_csv(self):
        """エフォートの生データをCSVファイルに保存"""
        if not hasattr(self, 'model') or self.model._df.empty:
            QMessageBox.warning(self, "Error", "No data to save")
            return
        
        # ファイル保存ダイアログ
        player_name = self.effort_data.get('player_name', 'Unknown').replace(' ', '_')
        date_str = ""
        if self.effort_data.get('date'):
            date = self.effort_data['date']
            if isinstance(date, (pd.Timestamp, datetime.datetime)):
                date_str = date.strftime("%Y%m%d_%H%M%S")
            else:
                date_str = str(date).replace(' ', '_').replace(':', '')
        
        default_filename = f"effort_{player_name}_{date_str}.csv"
        
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save CSV",
            default_filename,
            "CSV Files (*.csv)"
        )
        
        if not path:
            return
        
        try:
            # DataFrameをCSVに保存
            self.model._df.to_csv(path, index=False, encoding="utf-8-sig")
            QMessageBox.information(self, "Save Complete", f"CSV file saved:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", f"Failed to save CSV file:\n{e}")
    
class KPIPage(QWidget):
    """
    KPIページ
    - データ処理ロジック（DB 取得）は _load_data 系に集約
    - UI 構築は _build_ui で担当
    """

    # ------------------------------------------------------------------
    # 初期化
    # ------------------------------------------------------------------
    def __init__(self, query: str, stacked_widget, user_ids, start_jst=None, end_jst=None):
        super().__init__()

        self._base_query = query
        self.stacked_widget = stacked_widget
        self._user_ids = list(map(int, user_ids))
        self._start_jst = start_jst  # ログ用
        self._end_jst = end_jst      # ログ用

        self._settings = _load_json(SETTINGS_PATH)
        # kpi.jsonを読み込む
        self._interval_config = _load_json(KPI_INTERVALS_PATH) or {}
        
        # versionチェック: "ver2" または未設定（Web版kpi.json互換）を許容
        config_version = self._interval_config.get("version")
        if config_version is not None and config_version != "ver2":
            QMessageBox.warning(
                self,
                "Version Warning",
                f"kpi.json version is '{config_version}' (expected 'ver2' or none).\n"
                "Processing continues but some features may not work correctly."
            )
        
        # 地点名の検証
        invalid_positions = _validate_position_names(self._interval_config)
        if invalid_positions:
            valid_positions = ", ".join(TRACK_ORDER)
            QMessageBox.warning(
                self,
                "Invalid Position Names",
                f"Invalid position names found in kpi.json:\n\n{', '.join(invalid_positions)}\n\n"
                f"Valid position names are:\n{valid_positions}\n\n"
                f"Please check kpi.json and correct the position names."
            )
        


        # 利用可能なモードを取得（kpi.jsonから"version"以外のキーを取得）
        self._available_modes = self._get_available_modes()
        if not self._available_modes:
            QMessageBox.critical(
                self,
                "Configuration Error",
                "No modes found in kpi.json. Please check the configuration file."
            )
            import sys
            sys.exit(1)
        
        # time_mode 初期値（設定ファイルから読み込むか、最初のモードを使用）
        ui_mode = (self._settings.get("ui", {}).get("time_mode") or self._available_modes[0]).lower()
        # 利用可能なモードの中にあるか確認
        if ui_mode in self._available_modes:
            self.time_mode = ui_mode
        else:
            # 利用可能なモードにない場合は最初のモードを使用
            self.time_mode = self._available_modes[0]

        # データ読み込み（Data ロジック）
        self.df_all = pd.DataFrame()
        # CSVファイルパスの場合は_load_data_by_csv、それ以外は_load_data
        if isinstance(self._base_query, str) and (self._base_query.endswith('.csv') or self._base_query.startswith('CSV_FILE:')):
            csv_path = self._base_query.replace('CSV_FILE:', '') if self._base_query.startswith('CSV_FILE:') else self._base_query
            self._load_data_by_csv(csv_path)
        else:
            self._load_data()

        # UI 構築（UI ロジック）
        self._build_ui()
        
        # エフォート検出とテーブル更新
        self._detect_and_display_efforts()
        
        self.track_image_path = self._settings.get("image_path", "")
    
    def mousePressEvent(self, event):
        """マウスクリック時にウィンドウを前面に表示"""
        super().mousePressEvent(event)
        # 親ウィンドウを取得して前面に表示
        parent_window = self.window()
        if parent_window:
            parent_window.raise_()
            parent_window.activateWindow()
 
    # ------------------------------------------------------------------
    # データロジック
    # ------------------------------------------------------------------
    def _load_data(self):
        """DB から生データを取得する（シンプル版）"""
        df = get_df_from_db(self._base_query)

        if df.empty:
            QMessageBox.warning(
                self,
                "No Data",
                "No data found for the selected conditions.\nReturning to the previous page."
            )
            self.go_back()
            return
        
        # タイムスタンプをJST（naive）へ変換
        if "timestamp" in df.columns:
            df["timestamp"] = to_jst_naive(df["timestamp"])
        
        # デコーダ→地点名
        if "decoder_id" in df.columns:
            df["position"] = df["decoder_id"].map(translate_dict).fillna("Unknown")
        else:
            df["position"] = "Unknown"
        
        # 時系列でソート
        self.df_all = df.sort_values("timestamp").reset_index(drop=True)
        
        print(f"[データ読み込み] 完了: {len(self.df_all)}行")
    
    def _load_data_by_csv(self, csv_path: str):
        """CSVファイルから生データを取得する"""
        print(f"[CSV読み込み] ファイル: {csv_path}")
        
        # 複数のエンコーディングを試す
        encodings = ["utf-8-sig", "utf-8", "shift_jis", "cp932", "euc-jp"]
        df = None
        last_error = None
        
        for encoding in encodings:
            try:
                df = pd.read_csv(csv_path, encoding=encoding)
                break
            except UnicodeDecodeError as e:
                last_error = e
                continue
            except Exception as e:
                last_error = e
                continue
        
        if df is None:
            error_msg = f"CSVファイルの読み込みに失敗しました: {csv_path}\nすべてのエンコーディングで読み込みに失敗しました。"
            if last_error:
                error_msg += f"\n最後のエラー: {last_error}"
            QMessageBox.warning(
                self,
                "CSV読み込みエラー",
                error_msg
            )
            self.go_back()
            return
        
        if df.empty:
            QMessageBox.warning(
                self,
                "No Data",
                "CSVファイルにデータが含まれていません。\nReturning to the previous page."
            )
            self.go_back()
            return
        
        # タイムスタンプをJST（naive）へ変換
        # CSVの場合は既にJSTの可能性があるので、datetime型に変換してから処理
        if "timestamp" in df.columns:
            # 文字列の場合はdatetimeに変換
            if df["timestamp"].dtype == 'object':
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors='coerce')
            else:
                # 既にdatetime型の場合も確実にdatetime型にする
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors='coerce')
            
            # タイムゾーン情報がある場合はJSTに変換
            # pandasのDatetimeTZDtypeをチェック
            if hasattr(df["timestamp"].dtype, 'tz') and df["timestamp"].dtype.tz is not None:
                df["timestamp"] = to_jst_naive(df["timestamp"])
            # タイムゾーン情報がない場合はそのまま（既にJSTと仮定）
        
        # デコーダ→地点名
        if "decoder_id" in df.columns:
            df["position"] = df["decoder_id"].map(translate_dict).fillna("Unknown")
        elif "position" not in df.columns:
            # position列がない場合はUnknownを設定
            df["position"] = "Unknown"
        
        # 時系列でソート
        self.df_all = df.sort_values("timestamp").reset_index(drop=True)
        
        print(f"[CSV読み込み] 完了: {len(self.df_all)}行")
        

    def _get_available_modes(self) -> list[str]:
        """
        kpi.jsonから利用可能なモードのリストを取得する。
        "version"と"settings"以外のキーをモードとして返す。
        リスト形式（legacy-v1）とdict形式（Web版 {"mainKPI":..., "intervals":[...]}）の両方に対応。
        """
        if not self._interval_config:
            return []

        modes = []
        for key in self._interval_config.keys():
            if key in ("version", "settings"):
                continue
            value = self._interval_config[key]
            if isinstance(value, list):
                modes.append(key)
            elif isinstance(value, dict) and "intervals" in value:
                modes.append(key)

        return sorted(modes)

    def _display_kpi_columns(self) -> list[str]:
        """
        現在のモード(self.time_mode)について、
        kpi.json の start/end 定義から生成される KPI 列名を返す。
        リスト形式（legacy-v1）とdict形式（Web版）の両方に対応。
        """
        mode = (self.time_mode or "rolling").lower()
        cfg = self._interval_config or {}
        mode_data = cfg.get(mode, [])

        # Web版（dict形式）とlegacy-v1（list形式）の両方に対応
        if isinstance(mode_data, dict):
            entries = mode_data.get("intervals", [])
        elif isinstance(mode_data, list):
            entries = mode_data
        else:
            entries = []

        cols: list[str] = []
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            start = ent.get("start")
            end = ent.get("end")
            name = ent.get("name")
            if not start or not end:
                continue
            col_name = name if name else f"{start}-{end}"
            cols.append(col_name)

        return cols

    # ------------------------------------------------------------------
    # エフォート検出ロジック
    # ------------------------------------------------------------------
    def _assign_lap_numbers(self, effort_df: pd.DataFrame, start_type: str = None, start_time: pd.Timestamp = None) -> pd.DataFrame:
        """
        エフォート内のデータポイントにlap番号を割り振る
        
        - startがSB1の場合: 次のFPまでをlap0
        - startがFPの場合: 2回目のFPまでをlap0
        - その後はFPが来るたびにlap1, lap2, ...と割り振る
        
        Returns:
            lap_number列が追加されたDataFrame
        """
        result_df = effort_df.copy()
        
        if "timestamp" not in result_df.columns or "position" not in result_df.columns:
            # 必要な列がない場合はlap_number=0を設定
            result_df["lap_number"] = 0
            return result_df
        
        # timestamp順にソート
        result_df = result_df.sort_values("timestamp").reset_index(drop=True)
        
        # FPの位置を取得（時系列順）
        fp_rows = result_df[result_df["position"] == "FP"].sort_values("timestamp").reset_index(drop=True)
        
        # 初期化: すべての行にlap_number=0を設定
        result_df["lap_number"] = 0
        
        if fp_rows.empty:
            # FPがない場合はすべてlap0
            return result_df
        
        if start_type == "SB1" and start_time is not None:
            # SB1スタートの場合: 最初のFPまでをlap0、最初のFP以降をlap1から開始
            # start_time以降の最初のFPを探す
            first_fp_idx = None
            for idx, (_, fp_row) in enumerate(fp_rows.iterrows()):
                if fp_row["timestamp"] >= start_time:
                    first_fp_idx = idx
                    break
            
            if first_fp_idx is not None:
                first_fp_time = fp_rows.iloc[first_fp_idx]["timestamp"]
                # 最初のFPより前のデータポイントをlap0に設定（既に0なので変更不要）
                # 最初のFP以降（最初のFPを含む）をlap1から開始
                lap_num = 1
                for fp_idx in range(first_fp_idx, len(fp_rows)):
                    fp_time = fp_rows.iloc[fp_idx]["timestamp"]
                    # このFPから次のFPまでの範囲をlap_numに設定（このFPを含む）
                    if fp_idx < len(fp_rows) - 1:
                        next_fp_time = fp_rows.iloc[fp_idx + 1]["timestamp"]
                        mask = (result_df["timestamp"] >= fp_time) & (result_df["timestamp"] < next_fp_time)
                    else:
                        # 最後のFP以降（最後のFPを含む）
                        mask = result_df["timestamp"] >= fp_time
                    result_df.loc[mask, "lap_number"] = lap_num
                    lap_num += 1
        elif start_type == "FP" and start_time is not None:
            # FPスタートの場合: 2回目のFPまでをlap0
            # start_time以降の最初のFPを探す（これが1回目のFP）
            first_fp_after_start_idx = None
            for idx, (_, fp_row) in enumerate(fp_rows.iterrows()):
                if fp_row["timestamp"] > start_time:
                    first_fp_after_start_idx = idx
                    break
            
            if first_fp_after_start_idx is not None and first_fp_after_start_idx + 1 < len(fp_rows):
                # 2回目のFPを探す
                second_fp_time = fp_rows.iloc[first_fp_after_start_idx + 1]["timestamp"]
                # 2回目のFPまでのデータポイントをlap0に設定（既に0なので変更不要）
                # 2回目のFP以降をlap1から開始
                lap_num = 1
                for fp_idx in range(first_fp_after_start_idx + 1, len(fp_rows)):
                    fp_time = fp_rows.iloc[fp_idx]["timestamp"]
                    # このFPから次のFPまでの範囲をlap_numに設定
                    if fp_idx < len(fp_rows) - 1:
                        next_fp_time = fp_rows.iloc[fp_idx + 1]["timestamp"]
                        mask = (result_df["timestamp"] > fp_time) & (result_df["timestamp"] <= next_fp_time)
                    else:
                        # 最後のFP以降
                        mask = result_df["timestamp"] > fp_time
                    result_df.loc[mask, "lap_number"] = lap_num
                    lap_num += 1
            elif first_fp_after_start_idx is not None:
                # 2回目のFPがない場合（1回目のFPのみ）はすべてlap0のまま
                pass
        
        return result_df
    
    def _calculate_kpi_for_effort(self, effort_df: pd.DataFrame, start_type: str = None, start_time: pd.Timestamp = None) -> pd.DataFrame:
        """
        エフォートのDataFrameに対してKPI列を計算する
        
        Args:
            effort_df: エフォートのデータポイントから作成したDataFrame（各行が1つの位置のデータポイント）
            start_type: エフォートの開始タイプ（"SB1"または"FP"）
            start_time: エフォートの開始時刻
            
        Returns:
            KPI列が追加されたDataFrame
        """
        if effort_df.empty:
            return effort_df
        
        # 結果DataFrameをコピー
        result_df = effort_df.copy()
        
        # timestamp列とposition列が必要
        if "timestamp" not in result_df.columns or "position" not in result_df.columns:
            return result_df
        
        # timestamp順にソート
        result_df = result_df.sort_values("timestamp").reset_index(drop=True)
        
        # エフォート内のデータポイントにlap番号を割り振る
        result_df = self._assign_lap_numbers(result_df, start_type, start_time)
        
        # 現在のモードに基づいてKPI列を計算
        cfg = self._interval_config or {}
        mode = (self.time_mode or "").lower()
        mode_data = cfg.get(mode, [])

        # Web版（dict形式）とlegacy-v1（list形式）の両方に対応
        if isinstance(mode_data, dict):
            entries = mode_data.get("intervals", [])
        elif isinstance(mode_data, list):
            entries = mode_data
        else:
            entries = []

        if not entries:
            return result_df
        
        # トラック一周の地点順序（Web版 TRACK_ORDER と同じ）
        _FULL_ORDER = [
            "SB1", "FP", "0m", "60m", "AP1", "50m", "100m", "BP",
            "150m", "AP2", "200m", "FP_2nd", "0m_2nd", "BP_2nd",
        ]
        _FULL_IDX = {n: i for i, n in enumerate(_FULL_ORDER)}

        # Web版列名 → (物理地点名, 出現インデックス)
        # FP_2nd = 同一エフォート内でFPが2回目に現れるもの
        _POS_ALIAS2 = {
            "FP_start": ("FP", 0),
            "0m_start": ("0m", 0),
            "FP_2nd":   ("FP", 1),
            "0m_2nd":   ("0m", 1),
            "BP_2nd":   ("BP", 1),
        }

        def _parse_legacy_offset(pos_str):
            """Legacy形式 "0m+1" → ("0m", 1)"""
            if not pos_str:
                return None, 0
            m = re.match(r'^(.+?)\+(\d+)$', pos_str)
            if m:
                return m.group(1), int(m.group(2))
            return pos_str, 0

        def resolve_pos(col_name):
            """
            kpi.json の start/end 列名 → (物理地点名, 出現オフセット, full_track_index)
            Web形式 (FP_start, FP_2nd, ...) と legacy形式 (0m+1, FP+1, ...) の両方に対応。
            """
            if col_name in _POS_ALIAS2:
                phys, occ = _POS_ALIAS2[col_name]
            else:
                phys, occ = _parse_legacy_offset(col_name)
            # full_track_index: オリジナル列名 → 見つからなければ物理名で検索
            fidx = _FULL_IDX.get(col_name, _FULL_IDX.get(phys))
            return phys, occ, fidx

        def find_position_timestamp(physical_pos, occurrence=0):
            """
            エフォートデータ内で physical_pos が occurrence 番目(0始まり)に
            出現するタイムスタンプを返す。
            """
            if "lap_number" not in result_df.columns:
                rows = result_df[result_df["position"] == physical_pos]
                if occurrence < len(rows):
                    return rows.iloc[occurrence]["timestamp"]
                return None
            # lap_number がある場合は lap ごとに先頭を探す
            laps = sorted(result_df["lap_number"].unique())
            hit = 0
            for ln in laps:
                lap_rows = result_df[
                    (result_df["lap_number"] == ln) &
                    (result_df["position"] == physical_pos)
                ]
                if not lap_rows.empty:
                    if hit == occurrence:
                        return lap_rows.iloc[0]["timestamp"]
                    hit += 1
            return None

        for ent_idx, ent in enumerate(entries):
            if not isinstance(ent, dict):
                continue

            start_str = ent.get("start")
            end_str   = ent.get("end")
            name_str  = ent.get("name")

            if not start_str or not end_str:
                continue

            s_phys, s_occ, s_fidx = resolve_pos(start_str)
            e_phys, e_occ, e_fidx = resolve_pos(end_str)

            # Web版 ensure_interval_columns() の shift(-1) と同じルール:
            # end が start よりトラック順序で前(idx小)の場合、endの次の出現を使う
            if s_fidx is not None and e_fidx is not None and s_fidx > e_fidx:
                e_occ += 1

            # 列名はnameを使用（nameがなければ "start-end"）
            col_name = name_str if name_str else f"{start_str}-{end_str}"

            # すでに列があるなら再計算しない
            if col_name in result_df.columns:
                continue

            # 開始位置と終了位置のtimestampを取得
            start_time = find_position_timestamp(s_phys, s_occ)
            end_time   = find_position_timestamp(e_phys, e_occ)
            
            # 該当するデータがなければNaN
            if start_time is None or end_time is None:
                result_df[col_name] = math.nan
                continue
            
            # 時刻の差を計算（秒）
            try:
                time_diff = (end_time - start_time).total_seconds()
                kpi_value = round(time_diff, 3)
                # すべての行に同じ値を設定
                result_df[col_name] = kpi_value
            except Exception as e:
                print(f"[KPI計算エラー] {col_name}: {e}")
                result_df[col_name] = math.nan
        
        return result_df
    
    def _detect_and_display_efforts(self):
        """
        エフォートを検出してテーブルに表示する。
        
        ルール:
        - 0mを検出（起点0m）
        - 起点0mから5秒前の区間にSB1があればstartとしてその時刻を、SB1がなくFPがあればその時刻をstartに設定。どちらもなければエフォートIDを採番しない
        - 起点0m以降のFPをすべて取得。FPの間隔が30秒以上開く箇所を探し、その30秒以上開いたFPまでを同一エフォートデータとして保持
        - これを0m毎に繰り返す
        """
        efforts = []  # 最初に初期化
                
        # 必要な列の存在確認
        if "user_id" not in self.df_all.columns or "position" not in self.df_all.columns or "timestamp" not in self.df_all.columns:
            self.effort_table.setRowCount(0)
            self._efforts_data = []
            return
        
        # 選手名の列を確認
        has_first_name = "first_name" in self.df_all.columns
        has_last_name = "last_name" in self.df_all.columns
        
        # Date列を確認
        has_date = "Date" in self.df_all.columns
        
        # user_idが空欄のSB1データを取得（全選手で共有）
        sb1_no_user = self.df_all[
            (self.df_all["position"] == "SB1") & 
            (self.df_all["user_id"].isna() | (self.df_all["user_id"] == ""))
        ].copy()
        
        # 選手ごとにグループ化
        for user_id, group in self.df_all.groupby("user_id"):
            # user_idが空欄のグループはスキップ（SB1は後でマージする）
            if pd.isna(user_id) or user_id == "":
                continue
            
            # 選手名を取得
            player_name = "Unknown"
            if has_first_name or has_last_name:
                first_row = group.iloc[0]
                name_parts = []
                if has_first_name and pd.notna(first_row.get("first_name")):
                    name_parts.append(str(first_row["first_name"]))
                if has_last_name and pd.notna(first_row.get("last_name")):
                    name_parts.append(str(first_row["last_name"]))
                if name_parts:
                    player_name = " ".join(name_parts)
            
            
            # この選手のデータとuser_idが空欄のSB1データをマージ
            group_with_sb1 = pd.concat([group, sb1_no_user], ignore_index=True)
            
            # 時系列でソート
            group_sorted_all = group_with_sb1.sort_values("timestamp").reset_index(drop=True)
            
            # 0mの位置を持つ行を検出
            zero_m_rows = group_sorted_all[group_sorted_all["position"] == "0m"].copy()
            
            if zero_m_rows.empty:
                continue
            
            # 各0mについて独立にエフォートを検出
            for idx in range(len(zero_m_rows)):
                zero_m_row = zero_m_rows.iloc[idx]
                zero_m_time = zero_m_row["timestamp"]  # 起点0m
                
                if pd.isna(zero_m_time):
                    continue
                
                # 起点0mから5秒前の区間にSB1またはFPがあるかチェック
                start_time = None
                start_type = None
                
                # 全データから、起点0mの5秒前から起点0mまでの範囲でSB1またはFPを探す
                check_start_time = zero_m_time - pd.Timedelta(seconds=5)
                
                # 検索範囲内の行を取得
                mask = (group_sorted_all["timestamp"] >= check_start_time) & (group_sorted_all["timestamp"] <= zero_m_time)
                search_rows = group_sorted_all[mask]
                
                # SB1を優先して検索
                sb1_rows = search_rows[search_rows["position"] == "SB1"]
                if not sb1_rows.empty:
                    start_time = sb1_rows.iloc[-1]["timestamp"]  # 最後のSB1（0mに最も近い）
                    start_type = "SB1"
                else:
                    # FPを検索
                    fp_rows = search_rows[search_rows["position"] == "FP"]
                    if not fp_rows.empty:
                        start_time = fp_rows.iloc[-1]["timestamp"]  # 最後のFP（0mに最も近い）
                        start_type = "FP"
                
                # startが設定されていない場合はエフォートIDを採番しない
                if start_time is None:
                    continue
                
                # startから30秒以内のFPを順に追跡
                # 見つからなくなるまで繰り返し、最後のFPから30秒後までのデータをエフォートとして確定
                current_time = start_time
                last_fp_time = None
                
                # 起点0m以降のすべてのFPを時系列で取得
                fp_rows_after = group_sorted_all[
                    (group_sorted_all["timestamp"] > zero_m_time) & 
                    (group_sorted_all["position"] == "FP")
                ]
                fps_after_zero_m = fp_rows_after["timestamp"].tolist()
                
                # startから30秒以内のFPを順に追跡
                while True:
                    # current_timeから30秒以内のFPを探す
                    found_fp = None
                    for fp_time in fps_after_zero_m:
                        time_diff = (fp_time - current_time).total_seconds()
                        if 0 < time_diff <= 30:
                            found_fp = fp_time
                            break
                    
                    if found_fp is None:
                        # 30秒以内のFPが見つからなかった
                        if last_fp_time is None:
                            # FPが1つも見つからなかった場合
                            # startから30秒後までのデータを含める
                            end_time = start_time + pd.Timedelta(seconds=30)
                            
                            # startから30秒後までの間にあるすべてのデータポイントを取得
                            mask = (group_sorted_all["timestamp"] >= start_time) & (group_sorted_all["timestamp"] <= end_time)
                            effort_data = group_sorted_all[mask].sort_values("timestamp")
                            
                            # 辞書形式に変換
                            effort_data_points = effort_data.to_dict("records")
                            
                            start_date = zero_m_row.get("Date") if has_date else start_time
                            efforts.append({
                                "player_name": player_name,
                                "date": start_date,
                                "start_time": start_time,
                                "start_type": start_type,
                                "data_points": effort_data_points
                            })
                            break
                        else:
                            # 最後のFPから30秒後までのデータをエフォートとして確定
                            end_time = last_fp_time + pd.Timedelta(seconds=30)
                            
                            # startからend_timeまでの間にあるすべてのデータポイントを取得
                            mask = (group_sorted_all["timestamp"] >= start_time) & (group_sorted_all["timestamp"] <= end_time)
                            effort_data = group_sorted_all[mask].sort_values("timestamp")
                            
                            # 辞書形式に変換
                            effort_data_points = effort_data.to_dict("records")
                            
                            # エフォートを確定
                            start_date = zero_m_row.get("Date") if has_date else start_time
                            efforts.append({
                                "player_name": player_name,
                                "date": start_date,
                                "start_time": start_time,
                                "start_type": start_type,
                                "data_points": effort_data_points
                            })
                            break
                    else:
                        # 見つかったFPを次のcurrent_timeとして設定
                        last_fp_time = found_fp
                        current_time = found_fp
        
        # エフォートごとにKPIを計算
        print(f"[エフォート検出] 合計 {len(efforts)} 個のエフォートを検出")
        
        # KPI列のリストを取得（現在のモードに基づく）
        kpi_cols = self._display_kpi_columns()
        
        valid_efforts = []
        for effort_idx, effort in enumerate(efforts):
            if not effort.get("data_points"):
                continue
            
            # data_pointsをDataFrameに変換
            effort_df = pd.DataFrame(effort["data_points"])
            
            if effort_df.empty:
                continue
            
            # KPI列を計算（start_typeとstart_timeを渡す）
            effort_df_with_kpi = self._calculate_kpi_for_effort(
                effort_df, 
                start_type=effort.get("start_type"),
                start_time=effort.get("start_time")
            )
            
            # 計算したKPI列を含むdata_pointsに更新
            effort["data_points"] = effort_df_with_kpi.to_dict("records")
            
            # マイナスのKPI値が含まれるかチェック
            has_negative_kpi = False
            for kpi_col in kpi_cols:
                if kpi_col in effort_df_with_kpi.columns:
                    kpi_values = pd.to_numeric(effort_df_with_kpi[kpi_col], errors='coerce')
                    valid_values = kpi_values.dropna()
                    if len(valid_values) > 0:
                        # マイナスの値が含まれているかチェック
                        if (valid_values < 0).any():
                            has_negative_kpi = True
                            break
            
            # マイナスのKPI値が含まれていない場合のみ追加
            if not has_negative_kpi:
                valid_efforts.append(effort)
            else:
                print(f"[エフォート除外] マイナスのKPI値が含まれるエフォートを除外: {effort.get('player_name', 'Unknown')} - {effort.get('date', 'Unknown')}")
        
        # エフォートデータを保持
        self._efforts_data = valid_efforts.copy() if valid_efforts else []
        print(f"[エフォート検出] 有効なエフォート: {len(self._efforts_data)} 個（除外: {len(efforts) - len(self._efforts_data)} 個）")
        
        # エフォートテーブルを更新
        self._update_effort_table_display()

        # 操作ログ書き出し（比較スクリプト用）
        self._write_operation_log()

    def _write_operation_log(self):
        """エフォート検出結果を kpi_log.jsonl に追記する。比較スクリプト用。"""
        import json as _json
        if not hasattr(self, '_efforts_data') or not self._efforts_data:
            return
        kpi_cols = self._display_kpi_columns()
        effort_log = []
        for effort in self._efforts_data:
            kpi_vals = {}
            if effort.get("data_points"):
                edf = pd.DataFrame(effort["data_points"])
                for kc in kpi_cols:
                    if kc in edf.columns:
                        valid = pd.to_numeric(edf[kc], errors="coerce").dropna()
                        kpi_vals[kc] = round(float(valid.iloc[0]), 4) if len(valid) > 0 else None
                    else:
                        kpi_vals[kc] = None
            st = effort.get("start_time")
            dt = effort.get("date")
            effort_log.append({
                "player": effort.get("player_name"),
                "date": dt.isoformat() if hasattr(dt, "isoformat") else str(dt) if dt else None,
                "start": st.isoformat() if hasattr(st, "isoformat") else str(st) if st else None,
                "start_type": effort.get("start_type"),
                "kpi": kpi_vals,
            })
        entry = {
            "session_ts": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "params": {
                "start": self._start_jst,
                "end": self._end_jst,
                "ids": list(self._user_ids),
            },
            "mode": self.time_mode,
            "effort_count": len(effort_log),
            "efforts": effort_log,
        }
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kpi_log.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[ログ] {len(effort_log)} エフォート → {log_path}")

    def _update_effort_table_display(self):
        """エフォートテーブルの表示を更新（モード変更時にも呼び出される）"""
        if not hasattr(self, '_efforts_data') or not self._efforts_data:
            self.effort_table.setRowCount(0)
            return
        
        efforts = self._efforts_data
        
        # KPI列のリストを取得（現在のモードに基づく）
        kpi_cols = self._display_kpi_columns()
        
        # データ設定前にソートを無効化（列数変更やデータ設定時の干渉を防ぐ）
        self.effort_table.setSortingEnabled(False)
        
        # エフォートテーブルの列数を設定: 選手名、日時、[KPI列...]
        base_cols_before_kpi = 2  # 選手名、日時
        total_cols = base_cols_before_kpi + len(kpi_cols)
        self.effort_table.setColumnCount(total_cols)
        
        # ヘッダーラベルを設定
        headers = ["PlayerName", "Date"] + kpi_cols
        self.effort_table.setHorizontalHeaderLabels(headers)
        
        # ヘッダーのリサイズモードを設定（下のテーブルと同じ仕様）
        hh = self.effort_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        
        # 列幅を設定
        default_width = 160
        wider = {"PlayerName":220, "Date": 220}
        for i, header in enumerate(headers):
            self.effort_table.setColumnWidth(i, wider.get(header, default_width))
        
        # エフォートテーブルを更新
        self.effort_table.setRowCount(len(efforts))
        for idx, effort in enumerate(efforts):
            # 選手名（エフォートのインデックスをuserDataとして保存）
            player_item = QTableWidgetItem(str(effort["player_name"]))
            player_item.setData(Qt.UserRole, idx)  # エフォートのインデックスを保存
            self.effort_table.setItem(idx, 0, player_item)
            
            # 日時
            date_str = ""
            if effort["date"] is not None:
                if isinstance(effort["date"], (pd.Timestamp, datetime.datetime)):
                    date_str = effort["date"].strftime("%Y-%m-%d %H:%M:%S")
                else:
                    date_str = str(effort["date"])
            self.effort_table.setItem(idx, 1, QTableWidgetItem(date_str))
            
            # KPI列の値を表示
            if effort.get("data_points") and len(effort["data_points"]) > 0:
                effort_df = pd.DataFrame(effort["data_points"])
                
                for kpi_idx, kpi_col in enumerate(kpi_cols):
                    col_idx = base_cols_before_kpi + kpi_idx
                    
                    if kpi_col in effort_df.columns:
                        # KPI列の値を取得（エフォート全体で計算された値）
                        kpi_values = pd.to_numeric(effort_df[kpi_col], errors='coerce')
                        # NaN以外の値を取得（通常は1つの値のはず）
                        valid_values = kpi_values.dropna()
                        
                        if len(valid_values) > 0:
                            # 最初の有効な値を使用（通常は1つだけ）
                            kpi_value = valid_values.iloc[0]
                            # 数値型として設定（ソート時に数値として比較される）
                            item = NumericTableWidgetItem()
                            item.setData(Qt.EditRole, float(kpi_value))
                            # 表示用に小数点以下3桁でフォーマット
                            item.setText(f"{kpi_value:.3f}")
                            self.effort_table.setItem(idx, col_idx, item)
                        else:
                            # 空欄はカスタムクラスを使用（ソート時に最後に来る）
                            empty_item = NumericTableWidgetItem("")
                            empty_item.setData(Qt.EditRole, None)  # 明示的にNoneを設定
                            self.effort_table.setItem(idx, col_idx, empty_item)
                    else:
                        # 空欄はカスタムクラスを使用（ソート時に最後に来る）
                        empty_item = NumericTableWidgetItem("")
                        empty_item.setData(Qt.EditRole, None)  # 明示的にNoneを設定
                        self.effort_table.setItem(idx, col_idx, empty_item)
        
        # データ設定後にソートを再度有効化
        self.effort_table.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # UIロジック
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.setWindowTitle("LapApp Ver2 - Display KPIs")
        self.resize(980, 640)

        main_layout = QVBoxLayout()
        main_layout.addWidget(QLabel("KPIs"))

        # --- 上部バー（Show All, モード切替, Reload） ---
        top_row = QHBoxLayout()

        # モード選択ドロップダウン
        top_row.addWidget(QLabel("Mode:"))
        self.mode_combo = QComboBox()
        # 利用可能なモードをドロップダウンに追加
        for mode in self._available_modes:
            # モード名を表示用に整形（最初の文字を大文字に）
            display_name = mode.capitalize()
            self.mode_combo.addItem(display_name, userData=mode)
        
        # 現在のモードを選択
        current_index = self._available_modes.index(self.time_mode) if self.time_mode in self._available_modes else 0
        self.mode_combo.setCurrentIndex(current_index)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_combo_changed)
        
        top_row.addWidget(self.mode_combo)

        self.btnReload = QPushButton("Updata to Latest", self)
        self.btnReload.clicked.connect(self._reload_kpi)
        top_row.addWidget(self.btnReload)
        
        self.btnEditKpiJson = QPushButton("Edit KPI Setting", self)
        self.btnEditKpiJson.clicked.connect(self._open_kpi_json_editor)
        top_row.addWidget(self.btnEditKpiJson)

        # ショートカット: Ctrl+R でリロード
        reload_action = QAction(self)
        reload_action.setShortcut("Ctrl+R")
        reload_action.triggered.connect(self._reload_kpi)
        self.addAction(reload_action)

        main_layout.addLayout(top_row)


        # --- エフォートテーブル ---
        effort_label = QLabel("Effort List")
        main_layout.addWidget(effort_label)
        
        self.effort_table = QTableWidget(0, 2)
        self.effort_table.setHorizontalHeaderLabels(["Player", "Date"])

        hh = self.effort_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.effort_table.setAlternatingRowColors(True)
        self.effort_table.verticalHeader().setVisible(True)
        self.effort_table.verticalHeader().setDefaultSectionSize(26)
        self.effort_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.effort_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.effort_table.setSortingEnabled(True)
        main_layout.addWidget(self.effort_table)
        
        # 生データを確認ボタン
        btn_view_raw_data = QPushButton("View Raw Data for Selected Effort")
        btn_view_raw_data.clicked.connect(self._view_effort_raw_data)
        main_layout.addWidget(btn_view_raw_data)

        # 戻るボタン
        back_btn = QPushButton("← Back to Main")
        back_btn.clicked.connect(self.go_back)
        main_layout.addWidget(back_btn)

        self.setLayout(main_layout)

        # シグナル接続（UI→挙動）
        # モード変更は_on_mode_combo_changedで処理される

    # ---- モード変更 ----
    def _on_mode_combo_changed(self, index: int):
        """ドロップダウンでモードが変更されたときの処理"""
        mode = self.mode_combo.itemData(index)
        if mode:
            self._on_mode_changed(mode)
    
    def _on_mode_changed(self, mode: str):
        if mode == getattr(self, "time_mode", None):
            return
        self.time_mode = mode
        self._settings.setdefault("ui", {})["time_mode"] = mode
        _save_json(SETTINGS_PATH, self._settings)

        # モード変更時はKPIを再計算してからテーブルを更新
        if hasattr(self, '_efforts_data') and self._efforts_data:
            # KPI列のリストを取得（現在のモードに基づく）
            kpi_cols = self._display_kpi_columns()
            
            valid_efforts = []
            for effort_idx, effort in enumerate(self._efforts_data):
                if not effort.get("data_points"):
                    continue
                
                # data_pointsをDataFrameに変換
                effort_df = pd.DataFrame(effort["data_points"])
                
                if effort_df.empty:
                    continue
                
                # KPI列を再計算（新しいモードに応じたKPI列が計算される）
                effort_df_with_kpi = self._calculate_kpi_for_effort(
                    effort_df,
                    start_type=effort.get("start_type"),
                    start_time=effort.get("start_time")
                )
                
                # 計算したKPI列を含むdata_pointsに更新
                effort["data_points"] = effort_df_with_kpi.to_dict("records")
                
                # マイナスのKPI値が含まれるかチェック
                has_negative_kpi = False
                for kpi_col in kpi_cols:
                    if kpi_col in effort_df_with_kpi.columns:
                        kpi_values = pd.to_numeric(effort_df_with_kpi[kpi_col], errors='coerce')
                        valid_values = kpi_values.dropna()
                        if len(valid_values) > 0:
                            # マイナスの値が含まれているかチェック
                            if (valid_values < 0).any():
                                has_negative_kpi = True
                                break
                
                # マイナスのKPI値が含まれていない場合のみ追加
                if not has_negative_kpi:
                    valid_efforts.append(effort)
            
            # エフォートデータを更新（マイナスのKPI値が含まれるエフォートを除外）
            self._efforts_data = valid_efforts.copy() if valid_efforts else []

        # エフォートテーブルも更新（KPI列が変わるため）
        self._update_effort_table_display()

    # ---- 戻る ----
    def go_back(self):
        sw = self.stacked_widget
        idx = sw.indexOf(self)
        if idx != -1:
            sw.removeWidget(self)
        sw.setCurrentIndex(0)

    # ---- リロード ----
    def _reload_kpi(self):
        # データを再読込
        self._load_data()

        # エフォート検出とテーブル更新
        self._detect_and_display_efforts()

    def _reload_kpi_json(self):
        # KPI計算はエフォートごとに行うため、ここでは不要
        pass

    def _open_kpi_json_editor(self):
        """kpi.json編集ページへ遷移"""
        editor = KPIJsonEditorPage(self)
        self.stacked_widget.addWidget(editor)
        self.stacked_widget.setCurrentWidget(editor)
    
    def _view_effort_raw_data(self):
        """選択されたエフォートの生データを表示"""
        selected_rows = self.effort_table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.information(self, "選択エラー", "エフォートを選択してください")
            return
        
        # ソートされた状態でも正しいエフォートを取得するため、テーブルアイテムからインデックスを取得
        visual_row_idx = selected_rows[0].row()
        player_item = self.effort_table.item(visual_row_idx, 0)
        
        if not player_item:
            QMessageBox.warning(self, "エラー", "選択された行のデータを取得できませんでした")
            return
        
        # エフォートのインデックスを取得（UserRoleに保存されている）
        effort_idx = player_item.data(Qt.UserRole)
        
        if effort_idx is None:
            # UserRoleが設定されていない場合は、従来の方法で取得（後方互換性）
            effort_idx = visual_row_idx
        
        if not hasattr(self, '_efforts_data') or not self._efforts_data:
            QMessageBox.warning(self, "エラー", "エフォートデータが読み込まれていません")
            return
        
        if effort_idx < 0 or effort_idx >= len(self._efforts_data):
            QMessageBox.warning(
                self, 
                "エラー", 
                f"無効な行が選択されています\n行: {effort_idx}\nデータ数: {len(self._efforts_data)}"
            )
            return
        
        effort_data = self._efforts_data[effort_idx]
        
        # エフォート生データダイアログを別ウィンドウで開く（モーダルレス）
        raw_data_dialog = EffortRawDataPage(self, effort_data)
        raw_data_dialog.show()
