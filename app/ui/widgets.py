from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app import constants as C


# The same small, outlined vocabulary used by the approved A prototype. Rendering
# the SVG at several device ratios keeps it sharp without relying on an icon font.
_ICON_PATHS = {
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "folders": '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2"/><path d="M3 10h6l2 3h10v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    "history": '<path d="M3 11a9 9 0 1 1 2.5 7.3M3 4v7h7"/><path d="M12 7v5l3 2"/>',
    "activity": '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    "settings": '<path d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.5"/>',
    "sync": '<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m5 12 4 4 10-10"/>',
    "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "disk": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14h18m-4 3h1m-5 0h.01"/>',
    "phone": '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 5h4m-3 14h2"/>',
    "arrow": '<path d="M4 12h15m-5-5 5 5-5 5"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    "play": '<path d="m8 4 12 8-12 8Z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "edit": '<path d="m15 4 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14Z"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "restore": '<path d="M8 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-3M12 3h9v9m0-9L11 13"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
    "network": '<rect x="8" y="3" width="8" height="6" rx="1"/><path d="M12 9v6M4 15h16M4 15v3m8-3v3m8-3v3"/><path d="M2 18h4v3H2zm8 0h4v3h-4zm8 0h4v3h-4z"/>',
    "chevron": '<path d="m9 5 7 7-7 7"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2l-1.5 1v2m0 3h.01"/>',
    "file": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6M8 14h8m-8 3h5"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "link": '<path d="m10 13 4-4m-7 6-1 1a4 4 0 0 0 6 6l4-4a4 4 0 0 0 0-6m1-3 1-1a4 4 0 0 0-6-6L8 6a4 4 0 0 0 0 6" transform="translate(0 -2)"/>',
    "up": '<path d="m6 15 6-6 6 6"/>',
    "down": '<path d="m6 9 6 6 6-6"/>',
}


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#7b8caa", size: int = 20) -> QIcon:
    """Return an outlined SVG icon, with no external fonts or theme dependency."""
    size = max(8, int(size))
    ink = QColor(color)
    if not ink.isValid():
        ink = QColor("#7b8caa")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        f'viewBox="0 0 24 24" fill="none" stroke="{ink.name()}" '
        'stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">'
        f'{_ICON_PATHS.get(name, _ICON_PATHS["folder"])}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    result = QIcon()
    for ratio in (1, 2, 3):
        pixmap = QPixmap(size * ratio, size * ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        result.addPixmap(pixmap, QIcon.Mode.Normal)
    return result


def polish(widget: QWidget, **properties) -> None:
    """Update dynamic style properties without changing focus or layout state."""
    changed = False
    for key, value in properties.items():
        if widget.property(key) != value:
            widget.setProperty(key, value)
            changed = True
    if changed:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def label(text: str, role: str = "") -> QLabel:
    result = QLabel(str(text))
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setMinimumWidth(0)
    if role:
        result.setProperty("role", role)
    return result


def button(text: str, kind: str = "secondary", glyph: str = "") -> QPushButton:
    result = QPushButton(text)
    result.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    result.setAutoDefault(False)
    result.setProperty("kind", kind)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.setMinimumHeight(38)
    if glyph:
        color = "#ffffff" if kind == "primary" else "#3768e7" if kind == "link" else "#b45151" if kind == "danger" else "#7b8caa"
        result.setIcon(icon(glyph, color, 18))
        result.setIconSize(QSize(18, 18))
    if not text:
        result.setFixedWidth(38)
    return result


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)


class Badge(QLabel):
    def __init__(self, text: str = "", tone: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.setObjectName("badge")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_status(self, text: str, tone: str = "neutral") -> None:
        self.setText(text)
        polish(self, tone=tone)


class ElidedLabel(QLabel):
    """One-line paths/titles that cannot force an entire page wider."""

    def __init__(self, text: str = "", role: str = "", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if role:
            self.setProperty("role", role)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._elide()
        self.updateGeometry()

    def fullText(self) -> str:  # noqa: N802
        return self._full_text

    def _elide(self) -> None:
        available = max(0, self.contentsRect().width() - 2 * self.margin())
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, available))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()
            self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        margins = self.contentsMargins()
        return QSize(
            min(440, self.fontMetrics().horizontalAdvance(self._full_text) + 2) + margins.left() + margins.right(),
            self.fontMetrics().height() + 4 + margins.top() + margins.bottom(),
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        margins = self.contentsMargins()
        return QSize(margins.left() + margins.right(), self.fontMetrics().height() + 4 + margins.top() + margins.bottom())


class IconBox(QFrame):
    def __init__(self, name: str, tone: str = "blue", size: int = 42, parent=None):
        super().__init__(parent)
        self.setObjectName("iconWell")
        self.setProperty("tone", tone)
        self.setFixedSize(size, size)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        colors = {"blue": "#6487d7", "sand": "#b89555", "teal": "#4b9a91", "neutral": "#8193b0"}
        image = QLabel()
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image.setPixmap(icon(name, colors.get(tone, colors["neutral"]), round(size * 0.55)).pixmap(round(size * 0.55)))
        layout.addWidget(image)


class Header(QWidget):
    """Page heading with actions that move below the title in narrow windows."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(20)
        words = QWidget()
        words.setMinimumWidth(0)
        texts = QVBoxLayout(words)
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(7)
        self.title = label(title, "pageTitle")
        self.subtitle = label(subtitle, "subtitle")
        self.subtitle.setWordWrap(True)
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        self.action_widget = QWidget()
        self.actions = QHBoxLayout(self.action_widget)
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(9)
        self._layout.addWidget(words, 1)
        self._layout.addWidget(self.action_widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._stacked = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        stacked = self.width() < 720
        if stacked != self._stacked:
            self._stacked = stacked
            self._layout.setDirection(QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight)
            self._layout.setSpacing(15 if stacked else 20)
            self._layout.setAlignment(self.action_widget, Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag.AlignVCenter)


class StatCard(Card):
    def __init__(self, glyph: str, value: str, suffix: str, detail: str, parent=None):
        super().__init__(parent)
        self.setProperty("variant", "stat")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(17, 17, 17, 17)
        layout.setSpacing(13)
        self.well = IconBox(glyph, "neutral", 40)
        layout.addWidget(self.well, 0, Qt.AlignmentFlag.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(5)
        value_row = QHBoxLayout()
        value_row.setSpacing(7)
        self.value_label = ElidedLabel(value, "metric")
        self.value_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.suffix_label = label(suffix, "small")
        value_row.addWidget(self.value_label)
        value_row.addWidget(self.suffix_label, 0, Qt.AlignmentFlag.AlignBottom)
        value_row.addStretch(1)
        self.detail_label = label(detail, "small")
        self.detail_label.setWordWrap(True)
        words.addLayout(value_row)
        words.addWidget(self.detail_label)
        layout.addLayout(words, 1)

    def set_value(self, value: str, suffix: str = "", detail: str = "") -> None:
        self.value_label.setText(value)
        self.suffix_label.setText(suffix)
        self.suffix_label.setVisible(bool(suffix))
        self.detail_label.setText(detail)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.well.setVisible(self.width() >= 270)


class FlowLayout(QLayout):
    """A small native wrapping layout, for metadata, chips and task actions."""

    def __init__(self, parent=None, margin: int = 0, spacing: int = 8):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        visible = [item for item in self._items if not item.isEmpty()]
        margins = self.contentsMargins()
        width = sum(item.sizeHint().width() for item in visible)
        width += max(0, len(visible) - 1) * max(0, self.spacing())
        height = max((item.sizeHint().height() for item in visible), default=0)
        return QSize(width + margins.left() + margins.right(), height + margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        gap = max(0, self.spacing())
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            width = min(hint.width(), max(0, area.width()))
            if x > area.x() and x + width > area.right() + 1:
                x = area.x()
                y += row_height + gap
                row_height = 0
            height = item.heightForWidth(width) if item.hasHeightForWidth() else hint.height()
            height = max(hint.height(), height)
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, height)))
            x += width + gap
            row_height = max(row_height, height)
        return y + row_height - rect.y() + margins.bottom()


class DeviceIllustration(QWidget):
    """Decorative device mapping only; it deliberately encodes no online state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(160)
        self.setFixedHeight(110)
        self.setAccessibleName("电脑镜像到手机存储")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left = (self.width() - 168) / 2
        phone = QRectF(left + 6, 14, 43, 77)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#eef3fb"))
        painter.drawRoundedRect(phone.translated(5, 7), 10, 10)
        painter.setPen(QColor("#becde6"))
        painter.setBrush(QColor("#f4f8ff"))
        painter.drawRoundedRect(phone, 10, 10)
        painter.drawLine(QPoint(int(left + 22), 20), QPoint(int(left + 34), 20))
        painter.drawLine(QPoint(int(left + 24), 85), QPoint(int(left + 32), 85))
        icon("shield", "#6e8cc6", 22).paint(painter, QRect(int(left + 17), 40, 22, 22))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#b4c8e6"))
        for dx in (70, 80, 90):
            painter.drawEllipse(QPoint(int(left + dx), 53), 2, 2)
        painter.setPen(QColor("#c6d3e7"))
        painter.setBrush(QColor("#f7faff"))
        painter.drawRoundedRect(QRectF(left + 109, 30, 54, 47), 9, 9)
        icon("disk", "#8aa2c6", 27).paint(painter, QRect(int(left + 123), 40, 27, 27))
        painter.end()


def apply_theme(app: QApplication) -> None:
    """Always use the light A workspace, including native popup widgets.

    Windows may be using dark mode. A fixed Fusion palette prevents a dark
    combo popup / light foreground mismatch while retaining the OS title bar.
    """
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(14)
    app.setFont(font)
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#f6f8fc",
        QPalette.ColorRole.WindowText: "#253149",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f7f9fd",
        QPalette.ColorRole.Text: "#253149",
        QPalette.ColorRole.Button: "#ffffff",
        QPalette.ColorRole.ButtonText: "#253149",
        QPalette.ColorRole.Highlight: "#3768e7",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#253149",
        QPalette.ColorRole.ToolTipText: "#ffffff",
        QPalette.ColorRole.Link: "#3768e7",
        QPalette.ColorRole.LinkVisited: "#6658b5",
        QPalette.ColorRole.PlaceholderText: "#8a98ac",
        QPalette.ColorRole.Light: "#ffffff",
        QPalette.ColorRole.Midlight: "#e8edf5",
        QPalette.ColorRole.Mid: "#cbd5e4",
        QPalette.ColorRole.Dark: "#7b8ca6",
        QPalette.ColorRole.Shadow: "#253149",
    }
    for role, color in colors.items():
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette.setColor(group, role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#9aa8bb"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#f5f7fb"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor("#f1f4f9"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, QColor("#e3e9f4"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, QColor("#71829e"))
    app.setPalette(palette)
    style_path = C.resource_path("app/ui/style.qss")
    if not style_path.is_file():
        style_path = Path(__file__).with_name("style.qss")
    icon_folder = C.resource_path("app/resources/icons").as_posix()
    app.setStyleSheet(style_path.read_text(encoding="utf-8").replace("@ICON_DIR@", icon_folder))
