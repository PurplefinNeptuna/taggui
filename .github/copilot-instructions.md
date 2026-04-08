# TagGUI – Project Guidelines

## Project Overview

TagGUI is a PySide6 (Qt6) desktop app for editing image tags/captions used in generative AI training datasets. Tags are stored as plain `.txt` files beside images and auto-saved on every edit.

## Install and Run

```bash
uv sync
uv run python -m taggui.run_gui   # run the app
```

No formal test suite. Enable verbose logging with `TAGGUI_ENVIRONMENT=development`.

## Architecture

MVC-style layout inside `taggui/`:

| Layer | Location | Key classes |
|---|---|---|
| Data models | `models/` | `ImageListModel`, `ProxyImageListModel`, `TagCounterModel` |
| Widgets | `widgets/` | `MainWindow`, `ImageList`, `ImageTagsEditor`, `AllTagsEditor`, `AutoCaptioner` |
| ML / threading | `auto_captioning/` | `AutoCaptioningModel`, `CaptioningThread`, model subclasses |
| Utilities | `utils/` | `Image` dataclass, `settings.py`, `enums.py` |

`MainWindow` is the orchestrator — it holds references to all models and wires signals between widgets.

## Conventions

**Settings**: Always read via `get_settings().value('key', defaultValue=…, type=…)`. Defaults live in `DEFAULT_SETTINGS` in `taggui/utils/settings.py`. Never access `QSettings` directly.

**Adding a captioning model**:
1. Create `taggui/auto_captioning/models/your_model.py` subclassing `AutoCaptioningModel`.
2. Override only what differs: `dtype`, `image_mode`, `transformers_model_class`, `get_model_inputs()`, `generate_caption()`.
3. Register the class in `taggui/auto_captioning/models_list.py` — `MODELS` list (display name) + `get_model_class()` string-match block.

**Image dataclass** (`taggui/utils/image.py`): `Image` is a `@dataclass` with `path`, `dimensions`, `tags` (mutable list), and a lazily-cached `thumbnail`. Thumbnails are generated on first render and stored back on the instance.

**Undo/redo**: Implemented by storing a full snapshot of all image tags in a `deque(maxlen=32)`. Add entries whenever you mutate tags in batch to preserve undo support.

**Tag separator**: Always call `get_tag_separator()` from `settings.py`; never hardcode commas or newlines.

## Key Gotchas

- **Recursive directory scan**: `load_directory()` flattens images from all subdirectories into one list.
- **Model caching**: The loaded ML model lives on `MainWindow`, not the thread. Same model is reused between runs if device/dtype hasn't changed; call `del model; gc.collect()` when swapping.
- **Undo snapshot cost**: Each history entry clones tags for *all* images—keep batch operations to a single snapshot, not one per image.
- **Path comparison**: `get_file_paths()` converts `Path` objects to strings for matching `.txt` files because path comparison is slow on some systems; keep this pattern when adding file-matching logic.
- **Keyboard shortcuts**: `ShortcutRemover` filters `Ctrl+Z/Y` from text boxes so global undo/redo always works. Don't install conflicting shortcuts on `QLineEdit`/`QTextEdit` widgets.
- **Template variables** (`{tags}`, `{name}`, `{directory}`) are expanded by `replace_template_variables()` *before* the prompt reaches a model. Escape literal braces as `\{`.
