"""執行方式：python -m ui_qt"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from ui_qt import app_log, i18n
from ui_qt.main_window import MainWindow


def main():
    # 最先啟動記錄：之後任何一步出錯、卡死都有記錄可查（見 ui_qt/app_log.py）。
    app_log.setup()
    # 縮放比例照 Windows 顯示設定的實際值（125%、150%…），不取整數：四捨五入會把 150% 當成 200%，
    # 1080p 螢幕只剩 960×540 可用。小數倍率下字靠下面的字型微調保持清晰，圖示照實際像素比重畫。
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    # Windows 原生佈景會自己補畫選取裝飾（例如目錄 ::branch 的藍底），樣式表蓋不掉；
    # Fusion 完全照樣式表畫。
    app.setStyle("Fusion")
    # 高解析度螢幕上 Qt 預設不做字型微調（hinting），中文筆畫落在半個像素上會糊；
    # 開完整微調讓筆畫對齊像素格線。樣式表只指定字型家族與大小，這個設定會被每個元件繼承。
    app_font = QFont("Microsoft JhengHei UI")
    app_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(app_font)
    # Qt 自己的中文翻譯（輸入框右鍵選單、訊息框按鈕）。
    i18n.install_qt_translation(app)
    # 卡死監看在建立視窗之前就啟動：建構主視窗、載入字型這些啟動步驟如果
    # 卡住，也要留得下呼叫堆疊。
    app_log.start_freeze_watchdog()
    window = MainWindow()
    window.show()
    exit_code = app.exec()
    app_log.log.info("===== 結束 =====（%s）", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
