"""統一的訊息框小工具。

直接呼叫 QMessageBox.information()/critical()/question() 在沒有載入 Qt
官方翻譯檔的情況下，按鈕文字會是英文的 OK/Yes/No，跟其餘全繁體中文的
介面不一致。這裡統一包一層，按鈕文字固定用中文。
"""

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QLabel, QMessageBox

from .widgets import dialog_frame


def info(parent, title: str, text: str):
    box = QMessageBox(QMessageBox.Icon.Information, title, text, parent=parent)
    box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def error(parent, title: str, text: str):
    box = QMessageBox(QMessageBox.Icon.Critical, title, text, parent=parent)
    box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def confirm(parent, title: str, text: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text, parent=parent)
    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    yes_button = box.addButton("確定", QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(yes_button)
    box.exec()
    return box.clickedButton() is yes_button


def decode_failure(parent, filename: str, encoding: str, choices: list, remember: dict | None = None):
    """嚴格解碼失敗時要怎麼辦。回傳（"retry", 編碼名稱）、（"lossy", None）或（None, None）＝不開。
    檔案還沒開成功，所以換編碼重讀要在這裡選，不能到「書籍資料 → 讀取編碼」改。
    remember：一次讀好幾個檔（合併）時給一個 dict，視窗多一個「後面的檔案也這樣處理」，勾了就把選擇記在裡面
    （"action"、"label"），呼叫端之後的檔案直接照做，不再一個一個問。"""
    dialog = QDialog(parent)
    dialog.setWindowTitle("編碼可能不符")
    layout, footer = dialog_frame(dialog, enter_submits=True)
    text = QLabel(f"以 {encoding.upper()} 解讀「{filename}」時有無法解碼的內容。\n\n"
                  "容錯開啟：解不開的字顯示成 �，存檔後永久遺失。")
    text.setWordWrap(True)
    layout.addWidget(text)
    combo = QComboBox()
    combo.addItems(choices)
    layout.addWidget(combo)
    remember_box = None
    if remember is not None:
        remember_box = QCheckBox("後面的檔案遇到同樣情況也這樣處理")
        layout.addWidget(remember_box)
    buttons = QDialogButtonBox()
    cancel_button = buttons.addButton("略過這個檔" if remember is not None else "取消",
                                      QDialogButtonBox.ButtonRole.RejectRole)
    lossy_button = buttons.addButton("容錯開啟", QDialogButtonBox.ButtonRole.DestructiveRole)
    retry_button = buttons.addButton("用這個編碼重新讀取", QDialogButtonBox.ButtonRole.AcceptRole)
    retry_button.setObjectName("primary")
    retry_button.setDefault(True)
    result = {"action": None}
    retry_button.clicked.connect(lambda: (result.update(action="retry"), dialog.accept()))
    lossy_button.clicked.connect(lambda: (result.update(action="lossy"), dialog.accept()))
    cancel_button.clicked.connect(dialog.reject)
    footer.addWidget(buttons)
    dialog.exec()
    label = combo.currentText() if result["action"] == "retry" else None
    if remember_box is not None and remember_box.isChecked():
        remember.update(action=result["action"], label=label)
    dialog.deleteLater()
    return result["action"], label
