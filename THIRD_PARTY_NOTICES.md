# 第三方元件授權說明（Third-party notices）

TXT Formatter 本身的授權見 [LICENSE](LICENSE)（保留所有權利）。發佈的程式（Releases 的 zip）另外包含下列第三方元件，
它們依各自原本的授權條款提供，不受 TXT Formatter 的授權限制。各授權的全文在 [licenses/](licenses/) 資料夾
（zip 裡也有同一份）。

| 元件 | 版本 | 授權 | 全文 | 原始碼 |
|---|---|---|---|---|
| Python（直譯器與標準函式庫） | 3.10.11 | PSF License 2.0（含 CNRI 等歷史授權） | licenses/Python-PSF.txt | https://www.python.org/downloads/source/ |
| PySide6 ／ shiboken6（Qt for Python） | 6.10.2 | LGPL-3.0 | licenses/LGPL-3.0.txt、licenses/GPL-3.0.txt | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt 6（PySide6 內含的 Qt 函式庫） | 6.10.2 | LGPL-3.0 | licenses/LGPL-3.0.txt、licenses/GPL-3.0.txt | https://download.qt.io/official_releases/qt/ |
| regex | 2026.9.10 | Apache-2.0 與 CNRI Python 1.6 | licenses/regex.txt、licenses/Python-PSF.txt | https://github.com/mrabarnett/mrab-regex |
| opencc-python-reimplemented（含 OpenCC 詞典資料） | 0.1.7 | Apache-2.0 | licenses/OpenCC-Apache-2.0.txt、licenses/OpenCC-NOTICE.txt | https://github.com/yichen0831/opencc-python 、https://github.com/BYVoid/OpenCC |

程式是用 PyInstaller 打包的。PyInstaller 的授權（GPL-2.0 加上例外條款）明文允許用它打包、散佈任何程式，
不會影響被打包程式的授權，所以這裡不另外列出。

## 關於 Qt（LGPL-3.0）

- 本程式使用的 PySide6 與 Qt 是官方發佈的版本，**未經修改**，以動態連結（DLL）的方式使用。
- 發佈的 zip 解壓縮後是一個資料夾，Qt 與 PySide6 的函式庫是 `_internal\PySide6\` 底下獨立的檔案
  （`Qt6Core.dll`、`Qt6Gui.dll`、`Qt6Widgets.dll`、`QtCore.pyd`……）。你可以把它們換成自己編譯、
  或其他相容版本的同名檔案，程式會改用你換上的版本。
- 上表列出了 PySide6 與 Qt 對應版本的原始碼下載位置。如果官方位置無法取得該版本的原始碼，可以透過 GitHub
  聯繫本專案，會提供一份（只收取合理的傳遞成本）。

---

# Third-party notices (English)

The license of TXT Formatter itself is in [LICENSE](LICENSE) (all rights reserved). The distributed program (the zip on
the Releases page) also contains the third-party components listed above, which are provided under their own
license terms. Full license texts are in [licenses/](licenses/) (the same copy is inside the zip).

**Qt (LGPL-3.0):** PySide6 and Qt are the official, unmodified releases, used through dynamic linking. After
extracting the zip, the Qt and PySide6 libraries are separate files under `_internal\PySide6\` (`Qt6Core.dll`,
`Qt6Gui.dll`, `Qt6Widgets.dll`, `QtCore.pyd`, ...); you may replace them with your own builds or other compatible
versions of the same files and the program will use them. The corresponding source code is available at the locations
listed in the table; if it cannot be obtained there, contact this project through GitHub for a copy (for no more than
the reasonable cost of physically performing the transfer).
