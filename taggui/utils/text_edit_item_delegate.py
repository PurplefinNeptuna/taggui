from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QStyledItemDelegate

from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.tag_classifier import TAG_TYPE_COLOR_KEYS, TagClassifier, TagType

_TAG_TYPE_COLOR_KEYS = TAG_TYPE_COLOR_KEYS  # backward-compat alias


class TextEditItemDelegate(QStyledItemDelegate):
    def __init__(self, parent=None,
                 classifier: TagClassifier | None = None):
        super().__init__(parent)
        self._classifier = classifier

    def set_classifier(self, classifier: TagClassifier) -> None:
        self._classifier = classifier

    def paint(self, painter, option, index):
        # Add some left padding.
        option.rect.adjust(4, 0, 0, 0)
        if self._classifier is not None:
            tag = index.data(Qt.ItemDataRole.EditRole) or ''
            tag_type = self._classifier.classify(tag)
            color_key = _TAG_TYPE_COLOR_KEYS.get(tag_type)
            if color_key is not None:
                settings = get_settings()
                color_str = settings.value(
                    color_key, DEFAULT_SETTINGS[color_key], type=str)
                color = QColor(color_str)
                if color.isValid():
                    palette = QPalette(option.palette)
                    palette.setColor(QPalette.ColorRole.Text, color)
                    palette.setColor(QPalette.ColorRole.HighlightedText, color)
                    option.palette = palette
        super().paint(painter, option, index)

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setFrameStyle(QFrame.Shape.NoFrame)
        editor.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setStyleSheet('padding-left: 3px;')
        editor.index = index
        return editor

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(size.height() + 8)
        return size

    def eventFilter(self, editor, event: QEvent):
        if (event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)
            self.parent().setCurrentIndex(
                self.parent().model().index(editor.index.row(), 0))
            self.parent().selectionModel().select(
                self.parent().model().index(editor.index.row(), 0),
                QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.parent().setFocus()
            return True
        # This is required to prevent crashing when the user clicks on another
        # tag in the All Tags list.
        if event.type() == QEvent.FocusOut:
            self.commitData.emit(editor)
            self.closeEditor.emit(editor)
            return True
        return False
