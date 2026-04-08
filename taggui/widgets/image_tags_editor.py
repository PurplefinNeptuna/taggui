from PySide6.QtCore import (QEvent, QItemSelectionModel, QModelIndex, QObject,
                            QPoint, QRunnable, QSize, QStringListModel,
                            QThreadPool, QTimer, Qt, Signal, Slot)
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QAbstractItemView, QCompleter, QDockWidget,
                               QHBoxLayout, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QVBoxLayout, QWidget)
from transformers import PreTrainedTokenizerBase

from models.proxy_image_list_model import ProxyImageListModel
from models.tag_counter_model import TagCounterModel
from utils.danbooru_tags import (DanbooruLoadThread, blended_search,
                                 resolve_csv_path)
from utils.image import Image
from utils.settings import DEFAULT_SETTINGS, get_settings
from utils.text_edit_item_delegate import TextEditItemDelegate
from utils.utils import get_confirmation_dialog_reply
from widgets.image_list import ImageList

MAX_TOKEN_COUNT = 75


def format_count(n: int) -> str:
    """Format a large integer as a compact shorthand string (e.g. 1200 → '1.2K')."""
    if n >= 1_000_000:
        s = f'{n / 1_000_000:.1f}'.rstrip('0').rstrip('.')
        return f'{s}M'
    if n >= 1_000:
        s = f'{n / 1_000:.1f}'.rstrip('0').rstrip('.')
        return f'{s}K'
    return str(n)


class _SearchSignals(QObject):
    # search_id is carried through so stale results can be discarded
    results_ready = Signal(int, list)


class _SearchWorker(QRunnable):
    """Runs blended_search off the main thread and emits results via signals."""

    def __init__(
        self,
        query: str,
        db,
        local_tags: list,
        limit,
        replace_underscores,
        signals: _SearchSignals,
        search_id: int,
    ):
        super().__init__()
        # Prevent Qt from deleting this QRunnable after run() so the Python
        # wrapper—and its reference to signals—stays alive.
        self.setAutoDelete(False)
        self._query = query
        self._db = db
        self._local_tags = local_tags
        self._limit = limit
        self._replace_underscores = replace_underscores
        self.signals = signals
        self._search_id = search_id

    def run(self):
        results = blended_search(
            self._query, self._db, self._local_tags,
            self._limit, self._replace_underscores)
        self.signals.results_ready.emit(self._search_id, results)


class DanbooruSuggestionPopup(QListWidget):
    """Overlay dropdown for Danbooru tag suggestions.

    Parented to the main window and positioned in its coordinate space so the
    OS compositor cannot override the position (fixes Wayland centering).
    FocusPolicy.NoFocus keeps keyboard focus on the input box at all times.
    """

    tag_selected = Signal(str)  # emits the canonical tag string to insert

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.hide()
        self.itemClicked.connect(
            lambda item: self.tag_selected.emit(
                item.data(Qt.ItemDataRole.UserRole)))

    def show_suggestions(
        self, suggestions: list[dict], input_box: 'QLineEdit'
    ):
        self.clear()
        settings = get_settings()
        for s in suggestions:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, s['canonical'])
            item.setSizeHint(QSize(0, 26))
            self.addItem(item)

            row_widget = QWidget()
            row_widget.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(6, 2, 6, 2)
            row_layout.setSpacing(8)

            display_label = QLabel(s['display'])
            display_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            row_layout.addWidget(display_label, stretch=1)

            local_count = s.get('local_count', 0)
            if local_count > 0:
                local_label = QLabel(format_count(local_count))
                local_label.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                local_color = settings.value(
                    'color_suggestion_local_count',
                    DEFAULT_SETTINGS['color_suggestion_local_count'],
                    type=str)
                local_label.setStyleSheet(f'color: {local_color};')
                row_layout.addWidget(local_label)

            danbooru_count = s.get('danbooru_count', 0)
            if danbooru_count > 0:
                danbooru_label = QLabel(format_count(danbooru_count))
                danbooru_label.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                danbooru_color = settings.value(
                    'color_suggestion_danbooru_count',
                    DEFAULT_SETTINGS['color_suggestion_danbooru_count'],
                    type=str)
                danbooru_label.setStyleSheet(f'color: {danbooru_color};')
                row_layout.addWidget(danbooru_label)

            self.setItemWidget(item, row_widget)

        if self.count() == 0:
            self.hide()
            return
        # Reparent to the top-level window so we can overlap dock widgets.
        # Using a child widget avoids OS compositor overriding our position.
        top = input_box.window()
        if self.parent() is not top:
            self.hide()
            self.setParent(top)
        # Position directly below the input box using main-window coordinates.
        pos = input_box.mapTo(top, QPoint(0, input_box.height()))
        width = max(input_box.width(), 350)
        row_h = self.sizeHintForRow(0)
        frame = self.frameWidth()
        height = min(self.count() * row_h + 2 * frame, 350)
        self.setGeometry(pos.x(), pos.y(), width, height)
        self.raise_()
        self.show()

    def move_selection(self, delta: int):
        """Move popup selection up (delta=-1) or down (delta=1)."""
        if self.count() == 0:
            return
        cur = self.currentRow()
        if cur < 0:
            new_row = 0 if delta > 0 else self.count() - 1
        else:
            new_row = (cur + delta) % self.count()
        self.setCurrentRow(new_row)

    def get_current_canonical(self) -> str | None:
        item = self.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def get_canonical_at(self, row: int) -> str | None:
        item = self.item(row)
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class TagInputBox(QLineEdit):
    tags_addition_requested = Signal(list, list)
    classifier_ready = Signal(object)  # emits TagClassifier once DB is loaded

    def __init__(self, image_tag_list_model: QStringListModel,
                 tag_counter_model: TagCounterModel, image_list: ImageList,
                 tag_separator: str):
        super().__init__()
        self.image_tag_list_model = image_tag_list_model
        self.image_list = image_list
        self.tag_separator = tag_separator

        self.setPlaceholderText('Add Tag')
        self.setStyleSheet('padding: 8px;')
        settings = get_settings()
        autocomplete_tags = settings.value(
            'autocomplete_tags',
            defaultValue=DEFAULT_SETTINGS['autocomplete_tags'], type=bool)

        if autocomplete_tags:
            self._suggestion_count = settings.value(
                'danbooru_suggestion_count',
                defaultValue=DEFAULT_SETTINGS['danbooru_suggestion_count'],
                type=int)
            self._replace_underscores = settings.value(
                'danbooru_replace_underscores',
                defaultValue=DEFAULT_SETTINGS['danbooru_replace_underscores'],
                type=bool)
            self._db = None
            self._tag_counter_model = tag_counter_model
            self._current_search_id = 0
            self._thread_pool = QThreadPool()
            self._thread_pool.setMaxThreadCount(1)
            # Persistent signals object and worker reference prevent Python GC
            # from collecting them while the background thread is still running.
            self._signals = _SearchSignals()
            self._signals.results_ready.connect(self._on_search_done)
            self._current_worker: _SearchWorker | None = None

            self._popup = DanbooruSuggestionPopup()
            self._popup.tag_selected.connect(self._on_popup_tag_selected)

            self._debounce = QTimer()
            self._debounce.setSingleShot(True)
            self._debounce.setInterval(150)
            self._debounce.timeout.connect(self._on_debounce_expired)
            self.textChanged.connect(lambda _: self._debounce.start())

            # Hide popup when the input box loses focus
            self.installEventFilter(self)

            csv_path = resolve_csv_path()
            self._load_thread = DanbooruLoadThread(csv_path)
            self._load_thread.loaded.connect(self._on_db_loaded)
            self._load_thread.load_failed.connect(self._on_db_load_failed)
            self._load_thread.start()

            self.completer = None
        else:
            self._popup = None
            self._debounce = None
            self._load_thread = None
            self.completer = QCompleter(tag_counter_model)
            self.setCompleter(self.completer)
            self.completer.activated.connect(lambda text: self.add_tag(text))
            # Clear the input box after the completer inserts the tag into it.
            self.completer.activated.connect(
                lambda: QTimer.singleShot(0, self.clear))

    # ------------------------------------------------------------------
    # Danbooru DB loading
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_db_loaded(self, db):
        self._db = db
        from utils.tag_classifier import TagClassifier
        self.classifier_ready.emit(TagClassifier(db))

    @Slot(str)
    def _on_db_load_failed(self, message: str):
        QMessageBox.warning(
            self,
            'Danbooru Tags',
            f'Could not load Danbooru tags CSV:\n{message}\n\n'
            'Danbooru tag suggestions are disabled. '
            'You can set the CSV path in Settings.')
        self._db = None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @Slot()
    def _on_debounce_expired(self):
        query = self.text().strip()
        if not query:
            if self._popup is not None:
                self._popup.hide()
            return
        self._current_search_id += 1
        # Snapshot local tags on the main thread before dispatching the worker
        local_tags = list(self._tag_counter_model.most_common_tags)
        worker = _SearchWorker(
            query, self._db, local_tags,
            self._suggestion_count, self._replace_underscores,
            self._signals, self._current_search_id)
        # Hold a Python reference so the worker isn't GC'd mid-run.
        self._current_worker = worker
        self._thread_pool.start(worker)

    @Slot(int, list)
    def _on_search_done(self, search_id: int, results: list):
        if search_id != self._current_search_id:
            return  # stale result; a newer search is in flight
        if self._popup is not None:
            self._popup.show_suggestions(results, self)

    # ------------------------------------------------------------------
    # Popup interaction
    # ------------------------------------------------------------------

    def _on_popup_tag_selected(self, canonical: str):
        self.add_tag(canonical)
        self.clear()
        if self._popup is not None:
            self._popup.hide()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj is self and event.type() == QEvent.Type.FocusOut:
            # NoFocus policy on the popup means clicking a suggestion item
            # does NOT cause FocusOut here. FocusOut only fires when the user
            # genuinely moves focus elsewhere, so it is safe to hide.
            if self._popup is not None:
                self._popup.hide()
        return False

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        is_enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)

        # Danbooru popup keyboard navigation
        if self._popup is not None and self._popup.isVisible():
            if key == Qt.Key.Key_Escape:
                self._popup.hide()
                return
            if key == Qt.Key.Key_Down:
                self._popup.move_selection(1)
                return
            if key == Qt.Key.Key_Up:
                self._popup.move_selection(-1)
                return
            if is_enter:
                if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                    # Ctrl+Enter: force-select the first popup item
                    canonical = self._popup.get_canonical_at(0)
                    if canonical:
                        self._on_popup_tag_selected(canonical)
                        return
                current = self._popup.get_current_canonical()
                if current is not None:
                    self._on_popup_tag_selected(current)
                    return
                # No item highlighted: fall through to add text-box content

        if not is_enter:
            super().keyPressEvent(event)
            return

        # Enter key: add tag (handles both popup-off and completer paths)
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and self.completer is not None
                and self.completer.popup().isVisible()):
            first_tag = self.completer.popup().model().data(
                self.completer.model().index(0, 0), Qt.ItemDataRole.EditRole)
            self.add_tag(first_tag)
        else:
            self.add_tag(self.text())
        self.clear()
        if self.completer is not None:
            self.completer.popup().hide()
        if self._popup is not None:
            self._popup.hide()

    def add_tag(self, tag: str):
        if not tag:
            return
        tags = tag.split(self.tag_separator)
        selected_image_indices = self.image_list.get_selected_image_indices()
        selected_image_count = len(selected_image_indices)
        if len(tags) == 1 and selected_image_count == 1:
            # Add an empty tag and set it to the new tag.
            self.image_tag_list_model.insertRow(
                self.image_tag_list_model.rowCount())
            new_tag_index = self.image_tag_list_model.index(
                self.image_tag_list_model.rowCount() - 1)
            self.image_tag_list_model.setData(new_tag_index, tag)
            return
        if selected_image_count > 1:
            if len(tags) > 1:
                question = (f'Add tags to {selected_image_count} selected '
                            f'images?')
            else:
                question = (f'Add tag "{tags[0]}" to {selected_image_count} '
                            f'selected images?')
            reply = get_confirmation_dialog_reply(title='Add Tag',
                                                  question=question)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.tags_addition_requested.emit(tags, selected_image_indices)


class ImageTagsList(QListView):
    def __init__(self, image_tag_list_model: QStringListModel):
        super().__init__()
        self.image_tag_list_model = image_tag_list_model
        self.setModel(self.image_tag_list_model)
        self._delegate = TextEditItemDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setWordWrap(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

    def keyPressEvent(self, event: QKeyEvent):
        """
        Delete selected tags when the delete key or backspace key is pressed.
        """
        if event.key() not in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            super().keyPressEvent(event)
            return
        rows_to_remove = [index.row() for index in self.selectedIndexes()]
        if not rows_to_remove:
            return
        remaining_tags = [tag for i, tag
                          in enumerate(self.image_tag_list_model.stringList())
                          if i not in rows_to_remove]
        self.image_tag_list_model.setStringList(remaining_tags)
        min_removed_row = min(rows_to_remove)
        remaining_row_count = self.image_tag_list_model.rowCount()
        if min_removed_row < remaining_row_count:
            self.select_tag(min_removed_row)
        elif remaining_row_count:
            # Select the last tag.
            self.select_tag(remaining_row_count - 1)

    def select_tag(self, row: int):
        # If the current index is not set, using the arrow keys to navigate
        # through the tags after selecting the tag will not work.
        self.setCurrentIndex(self.image_tag_list_model.index(row))
        self.selectionModel().select(
            self.image_tag_list_model.index(row),
            QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def set_classifier(self, classifier) -> None:
        self._delegate.set_classifier(classifier)
        self.viewport().update()


class ImageTagsEditor(QDockWidget):
    def __init__(self, proxy_image_list_model: ProxyImageListModel,
                 tag_counter_model: TagCounterModel,
                 image_tag_list_model: QStringListModel, image_list: ImageList,
                 tokenizer: PreTrainedTokenizerBase, tag_separator: str):
        super().__init__()
        self.proxy_image_list_model = proxy_image_list_model
        self.image_tag_list_model = image_tag_list_model
        self.tokenizer = tokenizer
        self.tag_separator = tag_separator
        self.image_index = None

        # Each `QDockWidget` needs a unique object name for saving its state.
        self.setObjectName('image_tags_editor')
        self.setWindowTitle('Image Tags')
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea
                             | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tag_input_box = TagInputBox(self.image_tag_list_model,
                                         tag_counter_model, image_list,
                                         tag_separator)
        self.image_tags_list = ImageTagsList(self.image_tag_list_model)
        self.token_count_label = QLabel()
        # A container widget is required to use a layout with a `QDockWidget`.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.tag_input_box)
        layout.addWidget(self.image_tags_list)
        layout.addWidget(self.token_count_label)
        self.setWidget(container)

        # When a tag is added, select it and scroll to the bottom of the list.
        self.image_tag_list_model.rowsInserted.connect(
            lambda _, __, last_index:
            self.image_tags_list.selectionModel().select(
                self.image_tag_list_model.index(last_index),
                QItemSelectionModel.SelectionFlag.ClearAndSelect))
        self.image_tag_list_model.rowsInserted.connect(
            self.image_tags_list.scrollToBottom)
        # `rowsInserted` does not have to be connected because `dataChanged`
        # is emitted when a tag is added.
        self.image_tag_list_model.modelReset.connect(self.count_tokens)
        self.image_tag_list_model.dataChanged.connect(self.count_tokens)

    @Slot()
    def count_tokens(self):
        caption = self.tag_separator.join(
            self.image_tag_list_model.stringList())
        # Subtract 2 for the `<|startoftext|>` and `<|endoftext|>` tokens.
        caption_token_count = len(self.tokenizer(caption).input_ids) - 2
        if caption_token_count > MAX_TOKEN_COUNT:
            self.token_count_label.setStyleSheet('color: red;')
        else:
            self.token_count_label.setStyleSheet('')
        self.token_count_label.setText(f'{caption_token_count} / '
                                       f'{MAX_TOKEN_COUNT} Tokens')

    @Slot()
    def select_first_tag(self):
        if self.image_tag_list_model.rowCount() == 0:
            return
        self.image_tags_list.select_tag(0)

    def select_last_tag(self):
        tag_count = self.image_tag_list_model.rowCount()
        if tag_count == 0:
            return
        self.image_tags_list.select_tag(tag_count - 1)

    @Slot()
    def load_image_tags(self, proxy_image_index: QModelIndex):
        self.image_index = self.proxy_image_list_model.mapToSource(
            proxy_image_index)
        image: Image = self.proxy_image_list_model.data(
            proxy_image_index, Qt.ItemDataRole.UserRole)
        # If the string list already contains the image's tags, do not reload
        # them. This is the case when the tags are edited directly through the
        # image tags editor. Removing this check breaks the functionality of
        # reordering multiple tags at the same time because it gets interrupted
        # after one tag is moved.
        current_string_list = self.image_tag_list_model.stringList()
        if current_string_list == image.tags:
            return
        self.image_tag_list_model.setStringList(image.tags)
        self.count_tokens()
        if self.image_tags_list.hasFocus():
            self.select_first_tag()

    @Slot()
    def reload_image_tags_if_changed(self, first_changed_index: QModelIndex,
                                     last_changed_index: QModelIndex):
        """
        Reload the tags for the current image if its index is in the range of
        changed indices.
        """
        if (first_changed_index.row() <= self.image_index.row()
                <= last_changed_index.row()):
            proxy_image_index = self.proxy_image_list_model.mapFromSource(
                self.image_index)
            self.load_image_tags(proxy_image_index)
