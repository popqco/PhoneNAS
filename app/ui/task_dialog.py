from __future__ import annotations

import ntpath
import os
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from app import constants as C
from app.core.config import Config
from app.core.tasks import (
    MAX_INTERVAL_MINUTES, MAX_RETENTION_DAYS, MIN_INTERVAL_MINUTES, MIN_RETENTION_DAYS,
    MirrorTask, new_task, normalize_remote_path, validate_source, validate_tasks,
)
from app.ui.pages.settings import _operational
from app.ui.remote_folder_dialog import RemoteFolderDialog
from app.ui.widgets import Badge, Card, FlowLayout, IconBox, button, icon, label, polish


def _notice(text: str, glyph: str = "info") -> QFrame:
    result = QFrame()
    result.setObjectName("notice")
    layout = QHBoxLayout(result)
    layout.setContentsMargins(13, 11, 13, 11)
    layout.setSpacing(10)
    image = QLabel()
    image.setPixmap(icon(glyph, "#6f87b0", 18).pixmap(18))
    layout.addWidget(image, 0, Qt.AlignmentFlag.AlignTop)
    words = label(text, "bodyMuted")
    words.setWordWrap(True)
    layout.addWidget(words, 1)
    return result


def _tab_body() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setObjectName("dialogScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setMinimumWidth(0)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(2, 9, 12, 8)
    layout.setSpacing(17)
    scroll.setWidget(body)
    return scroll, body, layout


def _field(title: str, editor: QWidget, hint: str = "") -> QWidget:
    result = QWidget()
    result.setMinimumWidth(0)
    layout = QVBoxLayout(result)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(7)
    caption = label(title, "field")
    caption.setBuddy(editor)
    layout.addWidget(caption)
    layout.addWidget(editor)
    if hint:
        help_text = label(hint, "small")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
    return result


class TaskDialog(QDialog):
    """Edit a task draft; accepting returns a validated immutable task.

    This dialog never mutates Config, saves a file, or starts a sync. The owner
    remains responsible for committing the returned task and requesting preview.
    """

    def __init__(self, config: Config, task: MirrorTask | None = None, parent=None, *, operational: bool = True):
        super().__init__(parent)
        self.config = config
        self.operational = operational
        self.task: MirrorTask | None = None
        self._original = task
        self._draft = task if task is not None else new_task("")
        self._is_new = task is None
        self._name_edited = not self._is_new
        self._manual_targets = not self._is_new
        self._versions_manual = not self._is_new
        self._history_manual = not self._is_new
        self._remote_explicit = not self._is_new
        self._updating_targets = False
        self._purpose = self._draft.direction
        self._remote_dialog: RemoteFolderDialog | None = None
        self._loading = True
        self._previous_preset = ""
        self._custom_rules = "\n".join(self._draft.excludes)
        self._initial_custom_text: str | None = None
        self.setWindowTitle("添加同步任务 · PhoneNAS" if self._is_new else "编辑同步任务 · PhoneNAS")
        self.setModal(True)
        available = self.screen().availableGeometry()
        width, height = max(240, available.width() - 32), max(220, available.height() - 48)
        self.setMinimumSize(min(540, width), min(380, height))
        self.resize(min(740, width), min(700, height))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 23, 25, 20)
        layout.setSpacing(17)
        heading = QHBoxLayout()
        heading.setSpacing(13)
        heading.addWidget(IconBox("folder", size=46))
        words = QVBoxLayout()
        words.setSpacing(5)
        title = label("添加一个同步文件夹" if self._is_new else "编辑同步任务", "dialogTitle")
        title.setWordWrap(True)
        self.subtitle = label("选择用途：上传发布，或接收手机上已有的数据集。不是自动双向同步。", "subtitle")
        self.subtitle.setWordWrap(True)
        words.addWidget(title)
        words.addWidget(self.subtitle)
        heading.addLayout(words, 1)
        layout.addLayout(heading)
        self.error_label = label("", "error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setMinimumWidth(0)
        layout.addWidget(self.tabs, 1)
        self._build_folders_tab()
        self._build_schedule_tab()
        self._build_rules_tab()

        self.target_warning = _notice("先建立任务，再查看差异。查看差异不会改动电脑或手机里的文件。", "shield")
        layout.addWidget(self.target_warning)
        footer = QHBoxLayout()
        footer.setSpacing(10)
        footnote = label("此窗口不传输数据。启用任务允许手动执行；仅勾选“按计划自动执行”才参与自动计划，可能立即排队。请先查看差异。", "small")
        footnote.setWordWrap(True)
        footer.addWidget(footnote, 1)
        self.cancel_button = button("取消")
        self.save_button = button("添加任务" if self._is_new else "保存修改", "primary", "check")
        self.save_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

        self._loading = False
        self._preset_changed()
        self._purpose_changed()
        self._update_mapping()

    def _build_folders_tab(self) -> None:
        scroll, _, layout = _tab_body()
        self.direction_combo = QComboBox()
        self.direction_combo.setObjectName("taskPurpose")
        self.direction_combo.addItem("上传发布", "upload")
        self.direction_combo.addItem("下载接收", "download")
        self.direction_combo.setCurrentIndex(self.direction_combo.findData(self._draft.direction))
        self.direction_combo.currentIndexChanged.connect(self._purpose_changed)
        layout.addWidget(_field("任务用途", self.direction_combo, "上传发布更新手机镜像；下载接收更新这台电脑。交回修改需另行查看差异并确认。"))
        self.name_edit = QLineEdit(self._draft.name if not self._is_new else "")
        self.name_edit.setObjectName("taskName")
        self.name_edit.setPlaceholderText("例如：工作文件、产品文档")
        self.name_edit.setMaxLength(120)
        self.name_edit.textEdited.connect(self._name_changed)
        layout.addWidget(_field("任务名称", self.name_edit))

        source = QWidget()
        source_row = QHBoxLayout(source)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.setSpacing(9)
        self.local_edit = QLineEdit(self._draft.local_path)
        self.local_edit.setObjectName("taskSource")
        self.local_edit.setPlaceholderText("选择电脑上需要备份的文件夹")
        self.local_edit.textChanged.connect(self._source_changed)
        self.browse_button = button("选择文件夹…", "secondary", "folder")
        self.browse_button.clicked.connect(self._browse)
        source_row.addWidget(self.local_edit, 1)
        source_row.addWidget(self.browse_button)
        self.local_caption = label("本地来源目录 · 上传发布", "field")
        self.local_caption.setBuddy(self.local_edit)
        layout.addWidget(self.local_caption)
        layout.addWidget(source)
        self.local_hint = label("只读取本地来源；保存时会检查文件夹确实存在且可读。", "small")
        self.local_hint.setWordWrap(True)
        layout.addWidget(self.local_hint)

        mapping = Card()
        mapping.setProperty("variant", "mapping")
        mapping_layout = QVBoxLayout(mapping)
        mapping_layout.setContentsMargins(16, 14, 16, 14)
        mapping_layout.setSpacing(8)
        map_head = QHBoxLayout()
        destination_icon = QLabel()
        destination_icon.setPixmap(icon("arrow", "#7794cc", 19).pixmap(19))
        map_head.addWidget(destination_icon)
        self.remote_caption = label("手机镜像 · 上传发布的目标", "field")
        self.remote_caption.setWordWrap(True)
        map_head.addWidget(self.remote_caption, 1)
        map_head.addStretch(1)
        self.auto_badge = Badge("自动独立目录" if self._is_new else "任务独立目录", "blue")
        map_head.addWidget(self.auto_badge)
        self.remote_preview = label("", "mono")
        self.history_preview = label("", "mono")
        for preview in (self.remote_preview, self.history_preview):
            preview.setWordWrap(True)
            preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        mapping_layout.addLayout(map_head)
        mapping_layout.addWidget(self.remote_preview)
        mapping_layout.addWidget(self.history_preview)
        layout.addWidget(mapping)

        self.advanced_button = QToolButton()
        self.advanced_button.setText("高级 · 手动修改镜像与历史目录")
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setIcon(icon("chevron", "#8294b1", 16))
        self.advanced_button.setCheckable(True)
        self.advanced_button.setMinimumHeight(34)
        self.advanced_button.toggled.connect(self._show_advanced)
        layout.addWidget(self.advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced_widget = QWidget()
        advanced = QVBoxLayout(self.advanced_widget)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(13)
        self.remote_edit = QLineEdit(self._draft.remote_path)
        self.remote_edit.setObjectName("taskMirrorTarget")
        self.versions_edit = QLineEdit(self._draft.versions_path)
        self.versions_edit.setObjectName("taskHistoryTarget")
        self.remote_edit.textChanged.connect(self._targets_changed)
        self.versions_edit.textChanged.connect(self._versions_changed)
        remote_row = QWidget()
        row_layout = QHBoxLayout(remote_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.remote_edit, 1)
        self.browse_remote_button = button("浏览手机已有目录…", "secondary", "folder")
        self.browse_remote_button.clicked.connect(self._browse_remote)
        row_layout.addWidget(self.browse_remote_button)
        self.remote_hint = label("NAS 根目录下的相对路径；不能与任何任务的镜像 / 历史目录重叠。", "small")
        self.remote_hint.setWordWrap(True)
        advanced.addWidget(_field("手机镜像目录", remote_row))
        advanced.addWidget(self.remote_hint)
        advanced.addWidget(_field("手机历史目录（可编辑）", self.versions_edit, "上传发布或交回修改时，手机中被覆盖 / 删除的旧文件保留在这里。它不是完整时间点快照。"))
        self.reset_button = button("恢复自动分配", "link", "restore")
        self.reset_button.clicked.connect(self._reset_targets)
        advanced.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignLeft)
        advanced.addWidget(_notice("更换用途、目录或排除范围后，需要重新查看差异。旧目录的镜像与历史不会自动移动或删除。"))
        self.advanced_widget.setVisible(False)
        layout.addWidget(self.advanced_widget)
        self.local_history_edit = QLineEdit(self._draft.local_history_path)
        self.local_history_edit.setObjectName("taskLocalHistory")
        self.local_history_edit.setPlaceholderText("自动建议数据目录之外的同级历史目录")
        self.local_history_edit.textChanged.connect(self._local_history_changed)
        layout.addWidget(_field("本地历史目录 · 数据目录之外", self.local_history_edit, "接收手机更新前，本地被覆盖 / 删除的旧文件留在这里。上传端主动接收手机更新时也会使用。必须为绝对路径，且与所有数据目录隔离。"))
        self.enabled_check = QCheckBox("启用此任务（允许手动执行）")
        self.enabled_check.setChecked(self._draft.enabled)
        layout.addWidget(self.enabled_check)
        layout.addStretch(1)
        self.tabs.addTab(scroll, "文件夹")

    def _build_schedule_tab(self) -> None:
        scroll, _, layout = _tab_body()
        self.auto_sync_check = QCheckBox("按计划自动执行")
        self.auto_sync_check.setObjectName("taskAutoSync")
        self.auto_sync_check.setChecked(self._draft.auto_sync)
        layout.addWidget(self.auto_sync_check)
        columns = QGridLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setHorizontalSpacing(22)
        self.interval_spin = QSpinBox()
        self.interval_spin.setObjectName("taskInterval")
        self.interval_spin.setRange(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
        self.interval_spin.setSuffix(" 分钟 / 次")
        self.interval_spin.setValue(self._draft.interval_minutes)
        self.interval_spin.setMinimumHeight(44)
        self.interval_spin.setAccelerated(True)
        self.interval_spin.setEnabled(self.auto_sync_check.isChecked())
        self.auto_sync_check.toggled.connect(self.interval_spin.setEnabled)
        self.retention_spin = QSpinBox()
        self.retention_spin.setObjectName("taskRetention")
        self.retention_spin.setRange(MIN_RETENTION_DAYS, MAX_RETENTION_DAYS)
        self.retention_spin.setSuffix(" 天")
        self.retention_spin.setValue(self._draft.retention_days)
        self.retention_spin.setMinimumHeight(44)
        self.retention_spin.setAccelerated(True)
        columns.addWidget(_field("同步间隔", self.interval_spin, f"{MIN_INTERVAL_MINUTES}–{MAX_INTERVAL_MINUTES} 分钟；每个任务单独计时。"), 0, 0)
        columns.addWidget(_field("旧文件保留", self.retention_spin, "1–3650 天；仅清理此任务过期的旧文件。"), 0, 1)
        columns.setColumnStretch(0, 1)
        columns.setColumnStretch(1, 1)
        layout.addLayout(columns)
        layout.addWidget(_notice("只有已启用且勾选自动执行的任务会按计划排队。不勾选即为仅手动，仍可查看差异并手动执行。所有任务串行执行；暂停自动计划不停止当前传输。", "clock"))
        policy = Card()
        body = QVBoxLayout(policy)
        body.setContentsMargins(18, 18, 18, 18)
        body.setSpacing(9)
        body.addWidget(label("保留的是变化前的文件", "heading"))
        description = label("手机目标中的旧文件保留在手机历史，本地接收目录中的旧文件保留在本地历史。两种用途都保留本地历史设置，以便主动接收手机更新；历史文件不是完整时间点快照。", "bodyMuted")
        description.setWordWrap(True)
        body.addWidget(description)
        layout.addWidget(policy)
        layout.addStretch(1)
        self.tabs.addTab(scroll, "计划与保留")

    def _build_rules_tab(self) -> None:
        scroll, _, layout = _tab_body()
        self.preset_combo = QComboBox()
        self.preset_combo.setObjectName("taskExcludePreset")
        self.preset_combo.setMinimumHeight(43)
        self.preset_combo.addItem("推荐 · 排除构建产物，保留 Release", "recommended")
        self.preset_combo.addItem("不排除 · 同步所有文件", "none")
        self.preset_combo.addItem("自定义规则", "custom")
        index = self.preset_combo.findData(self._draft.exclude_preset)
        self.preset_combo.setCurrentIndex(max(0, index))
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        layout.addWidget(_field("排除规则", self.preset_combo, "只排除可重新生成的缓存与临时文件，不整目录排除 Release。"))
        self.rule_chips = QWidget()
        chips = FlowLayout(self.rule_chips, spacing=7)
        for text in ("node_modules", "build / obj", ".gradle", "__pycache__", "临时文件", "系统缩略图"):
            chip = label(text)
            chip.setObjectName("ruleChip")
            chips.addWidget(chip)
        layout.addWidget(self.rule_chips)
        self.rules_summary = label("", "bodyMuted")
        self.rules_summary.setWordWrap(True)
        layout.addWidget(self.rules_summary)
        self.rules_toggle = QToolButton()
        self.rules_toggle.setText(f"查看具体匹配规则 · {len(C.RECOMMENDED_EXCLUDES)} 条")
        self.rules_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.rules_toggle.setIcon(icon("chevron", "#8294b1", 16))
        self.rules_toggle.setCheckable(True)
        self.rules_toggle.toggled.connect(self._show_rules)
        layout.addWidget(self.rules_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.excludes_edit = QPlainTextEdit()
        self.excludes_edit.setObjectName("ruleText")
        self.excludes_edit.setPlaceholderText("每行一个 rclone 匹配规则，例如 **/node_modules/**")
        self.excludes_edit.setMinimumHeight(170)
        self.excludes_edit.setMaximumBlockCount(1000)
        layout.addWidget(self.excludes_edit)
        layout.addWidget(_notice("推荐规则适合代码项目。文档、照片等目录可以选择“不排除”；自定义规则按每行一个 rclone 匹配模式填写。", "shield"))
        layout.addStretch(1)
        self.tabs.addTab(scroll, "排除规则")

    def _purpose_changed(self, *_args) -> None:
        if self._loading:
            return
        direction = str(self.direction_combo.currentData())
        changed = direction != self._purpose
        self._purpose = direction
        receive = direction == "download"
        if self._is_new and changed:
            self.enabled_check.setChecked(not receive)
            if receive:
                self.auto_sync_check.setChecked(False)
            if receive and not self._remote_explicit:
                self._updating_targets = True
                self.remote_edit.clear()
                self.versions_edit.clear()
                self._updating_targets = False
                self._manual_targets = False
                self._versions_manual = False
        self.local_caption.setText("本地接收目录 · 下载接收" if receive else "本地来源目录 · 上传发布")
        self.local_edit.setPlaceholderText("选择这台电脑上已有的接收目录（可为空目录）" if receive else "选择这台电脑上需要发布的来源目录")
        self.local_hint.setText("手机镜像是来源；接收会更新此目录，原有文件可能被覆盖 / 删除。请选择已有目录，建议首次使用空目录。" if receive else "本地目录是来源，发布会更新手机镜像；主动接收手机更新需另外确认。")
        self.remote_caption.setText("手机已有数据集镜像 · 下载接收的来源" if receive else "手机镜像 · 上传发布的目标")
        self.remote_hint.setText("必须显式选择或填写手机上已有的数据集镜像相对路径；不会为下载接收自动创建空的随机目录。执行前会核对数据集标识与范围。" if receive else "NAS 根目录下的相对路径；不能与其他镜像 / 历史目录重叠。首次发布前会查看差异。")
        self.advanced_button.setText("手机镜像与历史目录" if receive else "高级 · 手动修改手机镜像与历史目录")
        self.advanced_button.setChecked(receive or not self._is_new or self._manual_targets)
        self.reset_button.setVisible(self._is_new and not receive)
        self.browse_remote_button.setEnabled(_operational(self))
        self.subtitle.setText("手机上的当前镜像 → 这台电脑的接收目录。不会自动交回本地修改。" if receive else "这台电脑的来源目录 → 手机上的当前镜像。不是自动双向同步。")
        self._update_auto_targets()
        self._update_mapping()

    def _browse(self) -> None:
        caption = "选择这台电脑上的接收目录" if self.direction_combo.currentData() == "download" else "选择电脑上的来源文件夹"
        folder = QFileDialog.getExistingDirectory(self, caption, self.local_edit.text())
        if folder:
            self.local_edit.setText(folder)

    def _browse_remote(self) -> None:
        if not _operational(self) or self._remote_dialog is not None:
            return
        dialog = RemoteFolderDialog(self.config, self, operational=self.operational)
        self._remote_dialog = dialog
        dialog.finished.connect(self._remote_selected)
        dialog.open()

    def _remote_selected(self, result: int) -> None:
        dialog = self._remote_dialog
        self._remote_dialog = None
        if dialog is None:
            return
        if result == QDialog.DialogCode.Accepted and dialog.remote_path:
            self.remote_edit.setText(dialog.remote_path)
        dialog.deleteLater()

    def _name_changed(self, _text: str) -> None:
        self._name_edited = True
        self._update_auto_targets()

    def _source_changed(self, text: str) -> None:
        if self._loading:
            return
        if self._is_new and not self._name_edited:
            self.name_edit.setText(ntpath.basename(text.rstrip("\\/")) or "")
        polish(self.local_edit, invalid=False)
        if not self._history_manual:
            try:
                suggestion = replace(self._draft, local_path=text.strip(), local_history_path="")
                self._updating_targets = True
                self.local_history_edit.setText(suggestion.local_history_path)
            except ValueError:
                pass
            finally:
                self._updating_targets = False
        self._update_auto_targets()
        self._update_mapping()

    def _update_auto_targets(self) -> None:
        if self._loading or self._manual_targets or self.direction_combo.currentData() == "download":
            return
        suggestion = new_task(self.local_edit.text().strip(), self.name_edit.text().strip())
        # Keep one ID for this draft even while its human-readable slug changes.
        suffix = suggestion.id[:8]
        self._updating_targets = True
        self.remote_edit.setText(suggestion.remote_path.replace(suffix, self._draft.id[:8]))
        if not self._versions_manual:
            self.versions_edit.setText(suggestion.versions_path.replace(suffix, self._draft.id[:8]))
        self._updating_targets = False
        self.auto_badge.set_status("自动独立目录", "blue")
        self._update_mapping()

    def _targets_changed(self, _text: str) -> None:
        if self._loading or self._updating_targets:
            return
        self._manual_targets = True
        self._remote_explicit = bool(self.remote_edit.text().strip())
        if not self._versions_manual:
            remote = self.remote_edit.text().strip()
            try:
                remote = normalize_remote_path(remote)
                suggestion = new_task("", remote_path=remote).versions_path
            except ValueError:
                suggestion = ""
            self._updating_targets = True
            self.versions_edit.setText(suggestion)
            self._updating_targets = False
        self.auto_badge.set_status("显式指定目录", "amber")
        self._update_mapping()

    def _versions_changed(self, _text: str) -> None:
        if self._loading or self._updating_targets:
            return
        self._versions_manual = True
        self._update_mapping()

    def _local_history_changed(self, _text: str) -> None:
        if not self._loading and not self._updating_targets:
            self._history_manual = True

    def _reset_targets(self) -> None:
        if self.direction_combo.currentData() == "download":
            return
        self._manual_targets = False
        self._versions_manual = False
        self._remote_explicit = False
        self._update_auto_targets()

    def _show_advanced(self, shown: bool) -> None:
        self.advanced_widget.setVisible(shown)
        self.advanced_button.setIcon(icon("down" if shown else "chevron", "#8294b1", 16))

    def _update_mapping(self) -> None:
        remote = self.remote_edit.text().strip()
        versions = self.versions_edit.text().strip()
        self.remote_preview.setText("手机镜像   /" + remote if remote else "手机镜像   请显式选择已有数据集")
        self.history_preview.setText("手机历史   /" + versions if versions else "手机历史   随镜像目录建议，可编辑")
        if self.direction_combo.currentData() == "download":
            self.auto_badge.set_status("已有数据集" if self._remote_explicit else "待选择", "blue" if self._remote_explicit else "amber")
        if self._original is not None:
            changed = (
                self.local_edit.text().strip() != self._original.local_path
                or self.remote_edit.text().strip() != self._original.remote_path
                or self.versions_edit.text().strip() != self._original.versions_path
                or self.direction_combo.currentData() != self._original.direction
            )
            message = "更换用途或目录后将重新查看差异；旧目录的文件不会移动或删除。" if changed else "保存只更新任务设置。执行前会查看差异并检查来源；保存不会改动任何数据文件。"
            notice_label = self.target_warning.findChildren(QLabel)[-1]
            notice_label.setText(message)

    def _preset_changed(self, *_args) -> None:
        if self._loading:
            return
        if self._previous_preset == "custom":
            self._custom_rules = self.excludes_edit.toPlainText()
        preset = self.preset_combo.currentData()
        self._previous_preset = preset
        self.rule_chips.setVisible(preset == "recommended")
        self.rules_toggle.setVisible(preset == "recommended")
        self.excludes_edit.setReadOnly(preset != "custom")
        if preset == "recommended":
            self.excludes_edit.setPlainText("\n".join(C.RECOMMENDED_EXCLUDES))
            self.rules_summary.setText("跳过构建缓存、临时文件和系统缩略图。Release 目录会保留；仍会应用具体文件匹配规则。")
            self.excludes_edit.setVisible(self.rules_toggle.isChecked())
        elif preset == "none":
            self.excludes_edit.clear()
            self.excludes_edit.setVisible(False)
            self.rules_summary.setText("不使用排除规则。所有文件都会参与同步，首次传输可能更多。")
        else:
            self.excludes_edit.setPlainText(self._custom_rules)
            if self._initial_custom_text is None:
                self._initial_custom_text = self.excludes_edit.toPlainText()
            self.excludes_edit.setVisible(True)
            self.rules_summary.setText("每行一个 rclone 匹配规则。请确认没有误排除需要备份的文件。")

    def _show_rules(self, shown: bool) -> None:
        if self.preset_combo.currentData() == "recommended":
            self.excludes_edit.setVisible(shown)
        self.rules_toggle.setIcon(icon("down" if shown else "chevron", "#8294b1", 16))

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        local = self.local_edit.text().strip()
        direction = str(self.direction_combo.currentData())
        if not name or not local:
            purpose = "接收目录" if direction == "download" else "来源目录"
            self._show_error(f"请填写任务名称，并选择电脑上的{purpose}。", focus_source=not local)
            return
        if direction == "download" and (not self._remote_explicit or not self.remote_edit.text().strip()):
            self._show_error("请显式选择或填写手机上已有的数据集镜像目录；下载接收不能使用自动创建的空目录。")
            self.tabs.setCurrentIndex(0)
            self.advanced_button.setChecked(True)
            self.remote_edit.setFocus()
            return
        preset = str(self.preset_combo.currentData())
        if preset == "recommended":
            patterns = tuple(C.RECOMMENDED_EXCLUDES)
        elif preset == "custom":
            text = self.excludes_edit.toPlainText()
            patterns = (self._draft.excludes if text == self._initial_custom_text
                        else tuple(line for line in text.split("\n") if line))
        else:
            patterns = ()
        drive = self.config.get("mount.drive", C.DEFAULT_DRIVE)
        try:
            remote = normalize_remote_path(self.remote_edit.text())
            versions = normalize_remote_path(self.versions_edit.text())
            history = self.local_history_edit.text().strip()
            scope_changed = self._original is not None and (
                direction != self._original.direction
                or local != self._original.local_path
                or remote != self._original.remote_path
                or versions != self._original.versions_path
                or history != self._original.local_history_path
                or patterns != self._original.excludes
            )
            dataset_id = self._draft.dataset_id if remote == self._draft.remote_path else new_task(
                local, name, direction=direction, remote_path=remote, versions_path=versions,
            ).dataset_id
            candidate = replace(
                self._draft,
                name=name,
                enabled=self.enabled_check.isChecked(),
                auto_sync=self.auto_sync_check.isChecked(),
                direction=direction,
                dataset_id=dataset_id,
                local_path=local,
                remote_path=remote,
                versions_path=versions,
                local_history_path=history,
                interval_minutes=self.interval_spin.value(),
                retention_days=self.retention_spin.value(),
                exclude_preset=preset,
                excludes=patterns,
                sync_identity=uuid.uuid4().hex if scope_changed else self._draft.sync_identity,
                last_sync_at="" if scope_changed else self._draft.last_sync_at,
                last_file_count=0 if scope_changed else self._draft.last_file_count,
            )
            others = tuple(item for item in self.config.tasks if item.id != candidate.id)
            validate_tasks((*others, candidate), mount_drive=drive)
            validate_source(candidate, mount_drive=drive)
        except (ValueError, OSError) as exc:
            self._show_error(str(exc) or "无法验证任务，请检查用途、数据目录与历史目录。")
            return
        self.task = candidate
        super().accept()

    def reject(self) -> None:
        if self._remote_dialog is not None:
            self._remote_dialog.reject()
        super().reject()

    def _show_error(self, message: str, focus_source: bool = False) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        if focus_source:
            self.tabs.setCurrentIndex(0)
            polish(self.local_edit, invalid=True)
            self.local_edit.setFocus()


class _DirectoryPicker(QFileDialog):
    """Qt's directory browser, extended to accept multiple selected directories."""

    def __init__(self, parent=None):
        super().__init__(parent, "选择多个文件夹")
        self.folders: list[str] = []
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setOption(QFileDialog.Option.ShowDirsOnly, True)
        self.setFileMode(QFileDialog.FileMode.Directory)
        self.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        self.setLabelText(QFileDialog.DialogLabel.Accept, "添加选中文件夹")
        self.setLabelText(QFileDialog.DialogLabel.FileName, "文件夹")
        self.resize(880, 600)
        for view in (*self.findChildren(QListView), *self.findChildren(QTreeView)):
            if view.objectName() in ("listView", "treeView"):
                view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
                view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

    def accept(self) -> None:
        folders: list[str] = []
        for view in (*self.findChildren(QListView), *self.findChildren(QTreeView)):
            if view.objectName() not in ("listView", "treeView") or not view.isVisible():
                continue
            selection = view.selectionModel()
            if selection is None:
                continue
            for index in selection.selectedRows(0):
                model = view.model()
                while hasattr(model, "mapToSource") and hasattr(model, "sourceModel"):
                    index = model.mapToSource(index)
                    model = model.sourceModel()
                if hasattr(model, "filePath"):
                    path = model.filePath(index)
                    if path and Path(path).is_dir() and path not in folders:
                        folders.append(path)
        if not folders:
            folders = [path for path in self.selectedFiles() if Path(path).is_dir()]
        if folders:
            self.folders = folders
            QDialog.accept(self)


class MultiFolderDialog(QDialog):
    """Collect sources only; the owner creates and validates independent tasks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.folders: list[str] = []
        self._paths: list[str] = []
        self.setWindowTitle("一次添加多个文件夹 · PhoneNAS")
        self.setModal(True)
        self.setMinimumSize(570, 420)
        self.resize(740, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 23, 25, 20)
        layout.setSpacing(16)
        layout.addWidget(label("一次添加多个文件夹", "dialogTitle"))
        subtitle = label("每个文件夹将创建一个独立任务，并自动分配独立的镜像与历史目录。", "subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.add_button = button("选择文件夹…", "secondary", "folders")
        self.remove_button = button("移除所选", "subtle", "close")
        self.count_badge = Badge("0 个文件夹", "blue")
        self.add_button.clicked.connect(self._choose)
        self.remove_button.clicked.connect(self._remove_selected)
        self.remove_button.setEnabled(False)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.remove_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.count_badge)
        layout.addLayout(toolbar)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setMinimumHeight(160)
        self.list.itemSelectionChanged.connect(lambda: self.remove_button.setEnabled(bool(self.list.selectedItems())))
        layout.addWidget(self.list, 1)
        hint = label("在选择器中按住 Ctrl 或 Shift 可多选同级目录；也可以反复添加不同位置的文件夹。", "small")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(_notice("这里只建立任务清单，不会立即复制文件。添加后可逐项修改计划、保留天数和排除规则。", "shield"))
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = button("取消")
        self.confirm_button = button("添加所选文件夹", "primary", "plus")
        self.confirm_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.confirm_button)
        layout.addLayout(footer)

    def _choose(self) -> None:
        picker = _DirectoryPicker(self)
        if self._paths:
            picker.setDirectory(str(Path(self._paths[-1]).parent))
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.add_folders(picker.folders)
        picker.deleteLater()

    def add_folders(self, folders: list[str]) -> None:
        seen = {os.path.normcase(os.path.abspath(path)) for path in self._paths}
        for raw in folders:
            path = os.path.normpath(str(raw))
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not Path(path).is_dir():
                continue
            seen.add(key)
            self._paths.append(path)
            item = QListWidgetItem(icon("folder", "#7593ce", 24), f"{ntpath.basename(path) or path}\n{path}")
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.list.addItem(item)
        self._update_count()

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            self._paths.remove(path)
            self.list.takeItem(self.list.row(item))
        self._update_count()

    def _update_count(self) -> None:
        self.count_badge.setText(f"{len(self._paths)} 个文件夹")
        self.confirm_button.setText(f"添加 {len(self._paths)} 个独立任务" if self._paths else "添加所选文件夹")
        self.confirm_button.setEnabled(bool(self._paths))

    def accept(self) -> None:
        if not self._paths:
            return
        self.folders = list(self._paths)
        super().accept()


# A descriptive alias for callers that prefer the action-oriented name.
AddFoldersDialog = MultiFolderDialog
