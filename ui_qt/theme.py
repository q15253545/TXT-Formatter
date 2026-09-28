"""色彩 token 與樣式表產生。

五種主題（簡約藍、簡約白、淺棕色、深色、純黑）各是一份 token；配色的角色見下面的「配色邏輯」。
UI 元件一律讀 token、不直接寫死色碼，切換主題只是換一份表再重新套用
樣式表。大量留白、柔和圓角、以底色深淺區分層次而不是明顯的框線，這個
方向維持不變。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    name: str          # 主題代號（存在 ui_state.json）
    label: str         # 選單上的名稱
    is_dark: bool      # 深色系：行尾空白標示等少數地方要調整透明度
    bg: str            # 視窗最底層背景
    surface: str        # 卡片／面板背景
    surface_hover: str   # 卡片內可互動元件的 hover 背景
    surface_active: str  # 按下／選取狀態
    border: str          # 極淡的分隔線，只在必要處使用
    text: str            # 主要文字（深灰，不用純黑）
    text_muted: str      # 次要文字／說明文字
    text_faint: str      # 佔位文字、停用狀態
    accent: str          # 強調色（主要按鈕、勾選框、開關、焦點框）
    accent_hover: str
    accent_text: str     # 強調色按鈕上的文字顏色
    selection_bg: str     # 目錄樹、表格、清單的選取列
    shadow: str           # 卡片陰影顏色（含透明度）
    icon: str             # 圖示線條顏色——跟 text_muted 分開，深色模式要更亮才看得清楚
    icon_hover: str
    warn_bg: str          # 「未完結」這類提醒用徽章的底色
    warn_text: str
    diff_text: str       # 自動修正預覽裡被改掉的字（紅色，深色模式要更亮）
    ok_bg: str            # 「已完結」這類完成狀態徽章的底色
    ok_text: str
    find_match_bg: str    # 搜尋命中的底色（其餘筆數）
    find_current_bg: str  # 目前這一筆命中的底色＝文字選取的顏色
    jump_bg: str          # 從對話框跳到本文時的整行底色：跟文字選取同色系但較淡，選字時才分得出範圍
    marker_text: str      # 不是原文的內容：顯示中的章節標記（[::]）、目錄的推定卷（UI_RULES.md）
    marker_bg: str
    button_bg: str        # 一般（次要）按鈕
    button_hover: str
    button_border: str
    checked_bg: str       # 工具列上「開啟中」的按鈕（排版設定、章節管理…）
    checked_border: str
    checked_text: str     # 開啟中按鈕的文字與圖示；也用在目錄 hover、展開中的摺疊鈕
    tree_selected_text: str  # 目錄選取列的文字
    title_text: str       # 本文裡的章節標題（粗體放大）；原則上跟主要文字同色
    toggle_track: str     # 右上角繁／簡切換
    toggle_knob: str
    toggle_text: str
    toggle_text_inactive: str
    toggle_shadow: str
    primary_bg: str       # 主要按鈕（選擇檔案、一鍵排版、匯出 TXT、套用格式…）
    primary_hover: str
    primary_text: str
    ad_mark_text: str     # 內容檢查「在本文標示顏色」：廣告
    note_mark_text: str   # 同上：作者感言、作品資訊
    control_text: str     # 按鈕、目錄項目平常的字色（簡約白用灰，滑鼠移上去才變黑）


# 配色邏輯：每個主題只有三種跟互動有關的顏色角色——
#   主要按鈕色 primary_*：選擇檔案、一鍵排版、匯出 TXT、套用格式的實心底色。
#   互動色 icon_hover：滑鼠移上去的文字、圖示、外框；鍵盤焦點框；開啟中按鈕的外框、
#       文字與圖示（checked_border／checked_text 一律等於它）；目錄滑鼠移上去的字色。
#       一定要跟 control_text 不同，移上去才看得出變化。
#   選取色 accent：勾選框、開關、分頁底線、連結。
# 一般按鈕（工具列、卡片、對話框）在同一個主題裡只有一種樣式（button_*）：
# 淺色主題是卡片底色＋細框，深色主題是實心；開啟中的底色 checked_bg 要跟它明顯不同。


# 簡約藍（預設）
SIMPLE_BLUE = Tokens(
    name="simple_blue", label="簡約藍", is_dark=False,
    bg="#F5F7FA", surface="#FFFFFF", surface_hover="#F6F9FF", surface_active="#F1F5FF", border="#E2E7EE",
    text="#243044", text_muted="#647084", text_faint="#A9B1BE",
    accent="#3869D8", accent_hover="#315CBE", accent_text="#FFFFFF",
    selection_bg="#EDF3FF", shadow="rgba(36, 48, 68, 40)", icon="#243044", icon_hover="#3869D8",
    warn_bg="#FBE7E7", warn_text="#B4383C", diff_text="#D92D20", ok_bg="#E4F4EA", ok_text="#1F7A4C",
    find_match_bg="#DCE8FC", find_current_bg="#B6CEF5", jump_bg="#EAF1FD",
    marker_text="#0F7B6C", marker_bg="#E3F2EF",
    button_bg="#FFFFFF", button_hover="#F6F9FF", button_border="#E2E7EE",
    checked_bg="#EDF3FF", checked_border="#3869D8", checked_text="#3869D8",
    tree_selected_text="#243044", title_text="#243044",
    toggle_track="#E9EDF2", toggle_knob="#FFFFFF", toggle_text="#243044", toggle_text_inactive="#A9B1BE",
    toggle_shadow="#1018271F",
    primary_bg="#3869D8", primary_hover="#315CBE", primary_text="#FFFFFF",
    ad_mark_text="#C2410C", note_mark_text="#7A4FCF",
    control_text="#243044",
)

# 簡約白：白底、黑灰色的字與按鈕，只用灰階
SIMPLE_WHITE = Tokens(
    name="simple_white", label="簡約白", is_dark=False,
    bg="#F9F9F9", surface="#FFFFFF", surface_hover="#F3F3F3", surface_active="#ECECEC", border="#E5E5E5",
    text="#0D0D0D", text_muted="#5D5D5D", text_faint="#A3A3A3",
    accent="#0D0D0D", accent_hover="#333333", accent_text="#FFFFFF",
    selection_bg="#ECECEC", shadow="rgba(0, 0, 0, 28)", icon="#5D5D5D", icon_hover="#0D0D0D",
    warn_bg="#FDECEC", warn_text="#C0362C", diff_text="#D92D20", ok_bg="#E7F5EC", ok_text="#1F7A4C",
    find_match_bg="#E1EBF7", find_current_bg="#C3D8F2", jump_bg="#EEF3FA",
    marker_text="#2F7D6D", marker_bg="#E6F2EF",
    button_bg="#FFFFFF", button_hover="#F3F3F3", button_border="#D9D9D9",
    checked_bg="#ECECEC", checked_border="#0D0D0D", checked_text="#0D0D0D",
    tree_selected_text="#0D0D0D", title_text="#0D0D0D",
    toggle_track="#ECECEC", toggle_knob="#FFFFFF", toggle_text="#0D0D0D", toggle_text_inactive="#5D5D5D",
    toggle_shadow="#0000001F",
    primary_bg="#0D0D0D", primary_hover="#333333", primary_text="#FFFFFF",
    ad_mark_text="#C2410C", note_mark_text="#6D4FC2",
    control_text="#5D5D5D",
)

# 淺棕色：米色紙張的閱讀感；除了主要按鈕、勾選框與反白以外都是駝色系
LIGHT_BROWN = Tokens(
    name="light_brown", label="淺棕色", is_dark=False,
    bg="#F1E7D2", surface="#FBF4E4", surface_hover="#F6EDDA", surface_active="#EADDC5", border="#E4DCC9",
    text="#4A4130", text_muted="#8C826C", text_faint="#B5AB95",
    accent="#0284C8", accent_hover="#0373AE", accent_text="#FFFFFF",
    selection_bg="#F0E5CC", shadow="rgba(74, 65, 48, 30)", icon="#837961", icon_hover="#8C6A3F",
    warn_bg="#F6DCCF", warn_text="#A8432A", diff_text="#C8341F", ok_bg="#DDEBD5", ok_text="#3F7A3A",
    find_match_bg="#C9DDF3", find_current_bg="#8EBDF0", jump_bg="#DCEAF7",
    marker_text="#7D6B3A", marker_bg="#ECE3C8",
    button_bg="#FBF4E4", button_hover="#F6EDDA", button_border="#DBCEB7",
    checked_bg="#E4D6BC", checked_border="#8C6A3F", checked_text="#8C6A3F",
    tree_selected_text="#4A4130", title_text="#4A4130",
    toggle_track="#EBE5D4", toggle_knob="#FDFBF6", toggle_text="#4A4130", toggle_text_inactive="#8C826C",
    toggle_shadow="#0000001F",
    primary_bg="#0284C8", primary_hover="#0373AE", primary_text="#FFFFFF",
    ad_mark_text="#B23A1E", note_mark_text="#6E5494",
    control_text="#4A4130",
)

# 深色（暖炭）：偏暖的炭黑底、磚紅色的互動色
DARK = Tokens(
    name="dark", label="深色", is_dark=True,
    bg="#1C1714", surface="#261F1B", surface_hover="#302824", surface_active="#3A312C", border="#3A322D",
    text="#EDE4DB", text_muted="#A39890", text_faint="#6F655E",
    accent="#A85A48", accent_hover="#BA6A56", accent_text="#FFF4EE",
    selection_bg="#3F3530", shadow="rgba(0, 0, 0, 140)", icon="#BDB0A6", icon_hover="#A85A48",
    warn_bg="#4A2622", warn_text="#F2A493", diff_text="#FF8F73", ok_bg="#27331F", ok_text="#A8CF8E",
    find_match_bg="#4E2B25", find_current_bg="#8A3B30", jump_bg="#3E211D",
    marker_text="#8FC7B4", marker_bg="#2B3531",
    button_bg="#3A322E", button_hover="#463C37", button_border="#3A322E",
    checked_bg="#4A2C26", checked_border="#A85A48", checked_text="#A85A48",
    tree_selected_text="#EDE4DB", title_text="#EDE4DB",
    toggle_track="#261F1B", toggle_knob="#4A403A", toggle_text="#FFF4EE", toggle_text_inactive="#A39890",
    toggle_shadow="#00000055",
    primary_bg="#A85A48", primary_hover="#BA6A56", primary_text="#FFF4EE",
    ad_mark_text="#F2926F", note_mark_text="#B9A5E6",
    control_text="#EDE4DB",
)

# 純黑：參考閱讀 App 的夜間模式；互動與選取是灰階、不用彩色，主要按鈕調暗成中灰（使用者選定的灰階方案）
BLACK = Tokens(
    name="black", label="純黑", is_dark=True,
    bg="#0A0A0A", surface="#151515", surface_hover="#2E2E2E", surface_active="#333333", border="#262626",
    text="#C8C8C8", text_muted="#7A7A7A", text_faint="#555555",
    accent="#7A7A7A", accent_hover="#8A8A8A", accent_text="#FFFFFF",
    selection_bg="#303030", shadow="rgba(0, 0, 0, 200)", icon="#A8A8A8", icon_hover="#F0F0F0",
    warn_bg="#3A1A1C", warn_text="#FF8A8F", diff_text="#FF6B72", ok_bg="#16241A", ok_text="#8CCB9A",
    find_match_bg="#2A2A2A", find_current_bg="#3E3E3E", jump_bg="#222222",
    marker_text="#8FC7B4", marker_bg="#16211E",
    button_bg="#262626", button_hover="#303030", button_border="#262626",
    checked_bg="#363636", checked_border="#F0F0F0", checked_text="#F0F0F0",
    tree_selected_text="#C8C8C8", title_text="#E8E8E8",
    toggle_track="#151515", toggle_knob="#3A3A3A", toggle_text="#F0F0F0", toggle_text_inactive="#7A7A7A",
    toggle_shadow="#00000055",
    primary_bg="#A8A8A8", primary_hover="#B8B8B8", primary_text="#111111",
    ad_mark_text="#E39A63", note_mark_text="#A99CDF",
    control_text="#C8C8C8",
)

# 選單上的順序：淺色系、分隔線、深色系
THEMES = {tokens.name: tokens for tokens in (SIMPLE_WHITE, SIMPLE_BLUE, LIGHT_BROWN, DARK, BLACK)}
DEFAULT_THEME = SIMPLE_BLUE.name


def theme_tokens(name: str | None) -> Tokens:
    return THEMES.get(name or "", THEMES[DEFAULT_THEME])


_active_tokens = SIMPLE_BLUE


def set_active_tokens(tokens: Tokens):
    """主視窗套用主題時記下來；對話框自己畫的圖示照這套配色。"""
    global _active_tokens
    _active_tokens = tokens


def active_tokens() -> Tokens:
    return _active_tokens


def blend(color: str, base: str, amount: float) -> str:
    """color 疊在 base 上、只透出 amount 那麼多（0～1）的顏色。"""
    top = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    bottom = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * amount + y * (1 - amount)):02X}" for x, y in zip(top, bottom))


def build_stylesheet(t: Tokens, chevron_closed_path: str = "", chevron_open_path: str = "",
                     check_path: str = "", chevron_up_path: str = "", minus_path: str = "") -> str:
    """chevron_*_path 是目錄樹展開／收合箭頭的暫存 PNG 路徑（見 icons.icon_file_path）。

    Qt 的 QSS 有個容易踩到的坑：只要對 ::branch 定義任何一條規則，整個
    branch 的繪製就會完全交給樣式表接管，不會再退回原生主題的預設箭頭。
    所以「有子節點」的展開／收合狀態必須自己提供圖片，不然一碰 ::branch
    就會連原生箭頭都不見，使用者完全看不出章節底下還有子章節。
    """
    return f"""
    * {{
        font-family: "Microsoft JhengHei UI", "Segoe UI", sans-serif;
        font-size: 14px;
        color: {t.text};
    }}

    QMainWindow, #centralWidget, QSplitter {{
        background: {t.bg};
    }}

    #headerBar {{
        background: {t.bg};
        border: none;
        border-bottom: 1px solid {t.border};
    }}

    #appTitle {{
        font-size: 15px;
        font-weight: 600;
        color: {t.text};
    }}

    #fileLabel {{
        font-size: 12px;
        color: {t.text_muted};
    }}
    #dialogIntro {{
        color: {t.text_muted};
    }}

    #metadataBar {{
        background: {t.bg};
        border-bottom: 1px solid {t.border};
    }}
    #badge {{
        background: {t.surface};
        border: 1px solid {t.border};
        color: {t.text_muted};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}
    #badgeWarn {{
        background: {t.warn_bg};
        color: {t.warn_text};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}
    #badgeOk {{
        background: {t.ok_bg};
        color: {t.ok_text};
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 8px;
    }}

    #card {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 14px;
    }}
    /* 每張卡片上緣的標題列：標題＋該卡片自己的動作圖示，用一條底線跟
       內容區分開，讓三張卡片看起來是同一套結構。 */
    #cardHeader {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {t.border};
    }}
    #cardTitle {{
        font-size: 15px;
        font-weight: 600;
        color: {t.text};
    }}
    #divider {{
        background: {t.border};
        border: none;
    }}
    #cardFooter {{
        background: transparent;
        border: none;
        border-top: 1px solid {t.border};
    }}
    #footerLabel {{
        font-size: 12px;
        color: {t.text_muted};
    }}
    /* 章節管理裡「檢查缺章」的結果區：比卡片底色深一階的小區塊。 */
    #reportPane {{
        background: {t.bg};
        border: 1px solid {t.border};
        border-radius: 10px;
    }}
    #reportTitle {{
        font-size: 12px;
        font-weight: 600;
        color: {t.text_muted};
    }}
    /* 尋找／取代面板（與格式選項共用左側卡片） */
    #findPanel {{
        background: transparent;
    }}
    QListWidget#findResults {{
        background: {t.bg};
        border: 1px solid {t.border};
        border-radius: 10px;
        outline: none;
        padding: 4px;
    }}
    QListWidget#findResults::item {{
        border-radius: 6px;
        margin: 1px 0px;
    }}
    QListWidget#findResults::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}
    #reportBody {{
        font-size: 13px;
        color: {t.text};
        background: transparent;
    }}

    QSplitter::handle {{
        background: transparent;
        width: 10px;
    }}

    QTreeWidget {{
        background: transparent;
        color: {t.control_text};
        border: none;
        outline: none;
        padding: 6px;
        show-decoration-selected: 0;
    }}

    QTreeWidget::item {{
        padding: 7px 8px;
        border-radius: 8px;
        margin: 1px 0px;
    }}

    QTreeWidget::item:hover {{
        color: {t.icon_hover};
    }}

    QTreeWidget::item:selected {{
        background: {t.selection_bg};
        color: {t.tree_selected_text};
    }}

    QTreeWidget::branch {{
        background: transparent;
        image: none;
        border: none;
    }}
    QTreeWidget::branch:selected {{
        /* 縮排／摺疊箭頭那一格不是章節標題本身，選取色只留給文字那顆圓角
           藥丸，這格要看起來沒被選到。

           這裡一定要指定實際色碼、不能寫 background: transparent——transparent
           會被 Qt 當成「這個屬性沒設定」而退回原生的整列選取填色，畫面上
           就會在縮排欄位多出一塊方角色塊，跟右邊的圓角藥丸接不起來。
           直接填卡片底色才是真的把它蓋掉。 */
        background: {t.surface};
        image: none;
        border: none;
    }}
    QTreeWidget::branch:has-children:closed {{
        image: url({chevron_closed_path});
    }}
    QTreeWidget::branch:has-children:closed:selected {{
        image: url({chevron_closed_path});
    }}
    QTreeWidget::branch:has-children:open {{
        image: url({chevron_open_path});
    }}
    QTreeWidget::branch:has-children:open:selected {{
        image: url({chevron_open_path});
    }}

    /* 可以選取文字的標籤（檔名）、唯讀文字框：反白跟本文、輸入框一樣，不用系統預設的藍色。 */
    QLabel, QTextEdit, QTextBrowser, QAbstractSpinBox {{
        selection-background-color: {t.find_current_bg};
        selection-color: {t.text};
    }}

    QPlainTextEdit {{
        background: transparent;
        border: none;
        padding: 18px 22px;
        selection-background-color: {t.find_current_bg};
        selection-color: {t.text};
    }}

    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.border};
        border-radius: 5px;
        min-height: 32px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t.text_faint};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    /* 不需要捲動時捲軸照樣佔位（widgets.reserve_scrollbar_gutter），只是看不見 */
    QScrollBar[idle="true"]::handle:vertical, QScrollBar[idle="true"]::handle:vertical:hover {{
        background: transparent;
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 12px;
        margin: 4px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.border};
        border-radius: 5px;
        min-width: 32px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}

    QToolButton {{
        background: transparent;
        border: none;
        border-radius: 10px;
        padding: 7px;
    }}
    QToolButton:hover {{
        background: {t.surface_hover};
    }}
    QToolButton:pressed {{
        background: {t.surface_active};
    }}
    /* 開關型的圖示按鈕（例如「顯示空格」）開啟中：淡藍底，不只靠圖示變色。 */
    QToolButton:checked {{
        background: {t.checked_bg};
    }}
    QToolButton:disabled {{
        opacity: 0.4;
    }}
    /* 工具列上的按鈕跟卡片、對話框裡的一般按鈕同一種樣式（button_*）：使用者
       一眼就看得出哪些是可以按的區塊，不必靠 hover 才浮出背景。有文字的按鈕是
       QPushButton（圖示＋文字會自動水平、垂直置中），純圖示的是 QToolButton，
       兩種都要涵蓋。 */
    QToolButton#toolbarButton, QPushButton#toolbarButton {{
        background: {t.button_bg};
        color: {t.control_text};
        border: 1px solid {t.button_border};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    /* 滑鼠移上去：能按的按鈕文字跟圖示一起變成 icon_hover。開啟中的按鈕照下面
       :checked 的顏色（寫在後面，同樣權重時後面的規則優先）。 */
    QToolButton#toolbarButton:hover, QPushButton#toolbarButton:hover {{
        background: {t.button_hover};
        border-color: {t.icon_hover};
        color: {t.icon_hover};
    }}
    QToolButton#toolbarButton:pressed, QPushButton#toolbarButton:pressed {{
        background: {t.surface_active};
    }}
    QToolButton#toolbarButton:checked, QPushButton#toolbarButton:checked {{
        background: {t.checked_bg};
        border-color: {t.checked_border};
        color: {t.checked_text};
    }}
    QToolButton#toolbarButton:disabled, QPushButton#toolbarButton:disabled {{
        color: {t.text_faint};
        border-color: {t.button_border};
        background: {t.button_bg};
    }}
    /* 檔案資訊列上的「書籍資料」展開鈕：跟工具列不同，不畫框、只用文字＋箭頭。 */
    QPushButton#barToggle {{
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 6px 10px;
    }}
    QPushButton#barToggle:hover {{
        background: {t.surface_hover};
        color: {t.icon_hover};
    }}
    QPushButton#barToggle:checked {{
        color: {t.checked_text};
    }}

    QPushButton {{
        background: {t.button_bg};
        color: {t.control_text};
        border: 1px solid {t.button_border};
        border-radius: 10px;
        padding: 8px 16px;
    }}
    QPushButton:hover {{
        background: {t.button_hover};
        border-color: {t.icon_hover};
        color: {t.icon_hover};
    }}
    QPushButton:pressed {{
        background: {t.surface_active};
    }}
    QPushButton:focus {{
        border: 1px solid {t.icon_hover};
    }}
    /* 帶下拉選單的按鈕（例如「快速插入」）：箭頭垂直置中放右側，跟目錄的箭頭同款。 */
    QPushButton#menuButton {{
        padding-right: 30px;
    }}
    /* 辨識章節的積木：只用主題的互動色（跟開啟中的按鈕同一組 checked_*）。
       欄與欄用很淡的互動色底隔開（外框、數字、分隔三欄），欄名底下一條互動色的線。 */
    QFrame#blockBand {{
        background: {blend(t.checked_border, t.surface, 0.08 if t.is_dark else 0.06)};
        border-radius: 8px;
    }}
    QLabel#blockHead {{
        font-weight: 600;
        color: {t.text};
    }}
    QFrame#blockRule {{
        background: {t.checked_border};
        border: none;
    }}
    /* each column's piece of the example title sits in its own box, so the row reads as a preview */
    QLabel#blockExample {{
        font-size: 18px;
        color: {t.text};
        padding: 4px 0;
        margin: 2px 0;
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 6px;
    }}
    QLabel#blockExample[empty="true"] {{
        color: {t.text_faint};
    }}
    QPushButton#blockChip {{
        padding: 3px 6px;
        border-radius: 6px;
        font-size: 13px;
    }}
    QPushButton#blockChip:checked {{
        background: {t.checked_bg};
        border-color: {t.checked_border};
        color: {t.checked_text};
    }}
    /* 內建組合固定的積木：一樣的實線框，只是淡一點 */
    QPushButton#blockChip:disabled {{
        color: {t.text_faint};
        border-color: {t.border};
        background: transparent;
    }}
    QPushButton#blockChip:checked:disabled {{
        background: {t.checked_bg};
        color: {t.text_muted};
        border-color: {t.text_faint};
    }}
    QLabel#confidence {{
        font-size: 11px;
        padding: 1px 6px;
        border-radius: 4px;
        background: {t.checked_bg};
        color: {t.checked_text};
    }}
    QFrame#resultBar {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 8px;
    }}
    QLabel#resultLine {{
        font-size: 15px;
    }}
    /* 一排插入用的小標籤（匯出檔名的變數）：比一般按鈕矮小，排成一列不會擠 */
    QPushButton#tagChip {{
        padding: 3px 10px;
        border-radius: 7px;
        font-size: 12px;
    }}
    QPushButton#menuButton::menu-indicator {{
        image: url({chevron_open_path});
        subcontrol-origin: padding;
        subcontrol-position: right center;
        right: 10px;
        width: 12px;
        height: 12px;
    }}
    QPushButton#primary {{
        background: {t.primary_bg};
        color: {t.primary_text};
        border: 1px solid {t.primary_bg};
        border-radius: 10px;
        padding: 8px 14px;
        font-weight: 500;
    }}
    QPushButton#primary:hover {{
        background: {t.primary_hover};
        border-color: {t.primary_hover};
        color: {t.primary_text};
    }}
    QPushButton#primary:focus {{
        border: 2px solid {t.text};
    }}
    QPushButton#primary:disabled {{
        background: {t.surface};
        border-color: {t.border};
        color: {t.text_faint};
    }}
    QPushButton#primary[split="left"] {{
        border-top-right-radius: 3px;
        border-bottom-right-radius: 3px;
    }}
    QPushButton#primary[split="right"] {{
        border-top-left-radius: 3px;
        border-bottom-left-radius: 3px;
        padding: 8px 6px;
    }}

    #versionLabel {{
        color: {t.text_faint};
        font-size: 12px;
        padding: 0px 10px 0px 6px;
    }}

    QStatusBar {{
        background: {t.bg};
        color: {t.text_muted};
        border-top: 1px solid {t.border};
        font-size: 12px;
        padding: 2px 4px;
    }}
    QStatusBar::item {{
        border: none;
    }}

    /* 輸入框、下拉框、數字框跟一般按鈕同高：都照字型高度加內距算（不寫死高度，換字型、縮放也對得齊）。 */
    QLineEdit {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 7px 10px;
        selection-background-color: {t.find_current_bg};
        selection-color: {t.text};
    }}
    QLineEdit:hover, QLineEdit:focus {{
        border: 1px solid {t.icon_hover};
    }}
    QLineEdit:disabled {{
        color: {t.text_faint};
    }}

    QMenu {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 6px 14px;
        border-radius: 7px;
        color: {t.control_text};
    }}
    QMenu::item:selected {{
        background: {t.surface_hover};
        color: {t.icon_hover};
    }}
    QMenu::item:disabled {{
        color: {t.text_faint};
    }}
    QMenu::separator {{
        height: 1px;
        background: {t.text_faint};
        margin: 6px 10px;
    }}

    QDialog {{
        background: {t.bg};
    }}

    /* 分頁：不套樣式的話 Fusion 會把分頁內容畫成灰黑底，字幾乎看不見。
       分頁本身做成底線式，跟工具列的「開著」狀態同一種強調色。 */
    QTabWidget::pane {{
        background: transparent;
        border: none;
        border-top: 1px solid {t.border};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {t.text_muted};
        border: none;
        border-bottom: 2px solid transparent;
        padding: 8px 16px;
        margin-right: 4px;
    }}
    QTabBar::tab:selected {{
        color: {t.accent};
        border-bottom: 2px solid {t.accent};
        font-weight: 600;
    }}
    QTabBar::tab:hover:!selected {{
        color: {t.icon_hover};
    }}

    /* 行內的文字連結式按鈕（例如「未收錄 5 行」）：不佔一般按鈕的高度與外框。 */
    QPushButton#inlineLink {{
        background: transparent;
        border: none;
        color: {t.accent};
        padding: 0 4px;
        min-height: 0;
    }}
    QPushButton#inlineLink:hover {{
        color: {t.icon_hover};
        text-decoration: underline;
    }}

    /* 工具視窗表格下面的前後文預覽：跟表格同一種框 */
    QTextEdit#contextPreview {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 6px 8px;
    }}

    QTableWidget {{
        background: {t.surface};
        gridline-color: {t.border};
        border: 1px solid {t.border};
        border-radius: 10px;
        outline: none;
    }}
    QTableWidget::item {{
        padding: 6px 8px;
        border: none;
    }}
    QTableWidget::item:selected {{
        background: {t.selection_bg};
        color: {t.text};
    }}
    QTableWidget::indicator {{
        width: 16px;
        height: 16px;
    }}
    QTableWidget::indicator:unchecked {{
        border: 1.5px solid {t.border};
        border-radius: 4px;
        background: {t.surface};
    }}
    QTableWidget::indicator:checked {{
        border: 1.5px solid {t.accent};
        border-radius: 4px;
        background: {t.accent};
        image: url({check_path});
    }}
    QHeaderView::section {{
        background: {t.surface};
        color: {t.text_muted};
        border: none;
        border-bottom: 1px solid {t.border};
        border-right: 1px solid {t.border};
        padding: 6px 8px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{
        background: {t.surface};
        border: none;
    }}

    /* 勾選框、開關的文字滑鼠移上去不變色（使用者決定），只有方框外框變互動色。 */
    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 17px;
        height: 17px;
        border-radius: 5px;
        border: 1.5px solid {t.border};
        background: {t.surface};
    }}
    QCheckBox::indicator:hover {{
        border-color: {t.icon_hover};
    }}
    QCheckBox::indicator:checked {{
        background: {t.accent};
        border-color: {t.accent};
        image: url({check_path});
    }}
    /* 區塊標題本身是勾選框（偵測類型、檢查項目）：字跟 #appTitle 一樣，部分勾選畫「－」 */
    QCheckBox#groupCheck {{
        font-size: 15px;
        font-weight: 600;
    }}
    QCheckBox::indicator:indeterminate {{
        background: {t.accent};
        border-color: {t.accent};
        image: url({minus_path});
    }}

    QComboBox {{
        /* combobox-popup: 0 讓選單從下拉框正下方展開；Fusion 預設的彈出方式
           會把選單疊在下拉框上面、蓋住目前選項，還會壓到上方的欄位標籤。 */
        combobox-popup: 0;
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 7px 30px 7px 10px;
    }}
    QComboBox:hover {{
        border-color: {t.icon_hover};
    }}
    QComboBox:focus {{
        border: 1px solid {t.icon_hover};
    }}
    QComboBox:disabled {{
        color: {t.text_faint};
    }}
    /* 只要改了 ::drop-down，Qt 就不再畫原生的下拉箭頭（跟目錄 ::branch 同一個
       坑），箭頭圖片必須自己給，不然下拉框看起來跟一般輸入框一模一樣。 */
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        border: none;
        width: 28px;
    }}
    QComboBox::down-arrow {{
        image: url({chevron_open_path});
        width: 12px;
        height: 12px;
    }}

    /* 數字框（重複段落「至少重複幾次」）：外觀跟下拉框一致，上下箭頭用同款圖示。 */
    QSpinBox {{
        background: {t.surface};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: 9px;
        padding: 5px 24px 6px 10px;
    }}
    QSpinBox:hover, QSpinBox:focus {{
        border-color: {t.icon_hover};
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        subcontrol-origin: border;
        width: 22px;
        border: none;
        background: transparent;
    }}
    QSpinBox::up-button {{
        subcontrol-position: top right;
        margin-top: 3px;
    }}
    QSpinBox::down-button {{
        subcontrol-position: bottom right;
        margin-bottom: 3px;
    }}
    QSpinBox::up-arrow {{
        image: url({chevron_up_path});
        width: 10px;
        height: 10px;
    }}
    QSpinBox::down-arrow {{
        image: url({chevron_open_path});
        width: 10px;
        height: 10px;
    }}

    /* 拉桿（重複段落「最短長度」）：軌道淡灰、已選的部分用強調色。
       拉桿本身要夠高，圓鈕（比軌道高）才不會被上下切掉。 */
    QSlider:horizontal {{
        min-height: 24px;
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {t.border};
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {t.accent};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {t.surface};
        border: 2px solid {t.accent};
        width: 12px;
        height: 12px;
        margin: -6px 0;
        border-radius: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.surface};
        border: 1px solid {t.border};
        padding: 4px;
        outline: 0;
    }}
    /* 清單類（下拉清單、右鍵選單、搜尋結果、目錄）同一套規則：平常 control_text，
       滑鼠移上去換互動色，選中的項目加淡底、字維持一般色（選中寫在後面，優先）。 */
    QComboBox QAbstractItemView::item {{
        min-height: 30px;
        padding: 0px 10px;
        border: none;
        border-radius: 6px;
        color: {t.control_text};
    }}
    QComboBox QAbstractItemView::item:hover {{
        background: {t.surface_hover};
        color: {t.icon_hover};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background: {t.selection_bg};
        color: {t.tree_selected_text};
    }}

    #panelScroll, #panelScrollContent {{
        background: transparent;
        border: none;
    }}

    #findBar {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 12px;
    }}
    QListWidget#findResults {{
        background: transparent;
        border: 1px solid {t.border};
        border-radius: 10px;
        padding: 4px;
        outline: none;
    }}
    QListWidget#findResults::item {{
        padding: 6px 8px;
        border-radius: 7px;
        margin: 1px 0px;
    }}
    QListWidget#findResults::item:hover {{
        background: {t.surface_hover};
        color: {t.icon_hover};
    }}
    QListWidget#findResults::item:selected {{
        background: {t.selection_bg};
        color: {t.tree_selected_text};
    }}
    /* 自訂章節規則 → 辨識格式的分類清單：跟目錄樹同一套 hover／選取色 */
    /* 辨識章節左邊的組合清單：每一列是自己畫的（名稱、信心、開關），選到的那一列加淡底 */
    /* 目錄是空的時候，目錄卡片最上面那段提示（加成辨識章節的組合） */
    QFrame#tocHint {{
        background: {t.selection_bg};
        border-radius: 8px;
    }}
    QFrame#comboPane {{
        border: none;
        border-right: 1px solid {t.border};
    }}
    QListWidget#comboList {{
        background: transparent;
        border: none;
        outline: none;
    }}
    QListWidget#comboList::item {{
        border-radius: 8px;
        margin: 1px 0px;
    }}
    QListWidget#comboList::item:hover {{
        background: {t.surface_hover};
    }}
    QListWidget#comboList::item:selected {{
        background: {t.selection_bg};
    }}

    QToolTip {{
        background: {t.text};
        color: {t.bg};
        border: none;
        border-radius: 6px;
        padding: 4px 8px;
    }}
    """
