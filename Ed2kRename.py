import errno, os, shutil, sys
from qtui import Ui_MainWindow
from Crypto.Hash import MD4
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QEvent, QRect, QPoint, QTimer
from PyQt5.QtGui import QColor, QCursor
from PyQt5.QtWidgets import (QApplication, QMainWindow, QFileDialog, QTableWidgetItem, QHeaderView,
                             QMessageBox, QPushButton, QSystemTrayIcon, QMenu, QAction, QToolButton,
                             QWidget, QStyle, QStyleOptionToolButton)


# 常见的视频文件扩展名，用于“选择目录”时筛选文件
VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.avi', '.wmv', '.rmvb', '.rm', '.mov', '.flv',
    '.ts', '.m2ts', '.mts', '.mpg', '.mpeg', '.m4v', '.webm', '.3gp',
    '.vob', '.asf', '.divx', '.f4v', '.ogv', '.m2v', '.dat', '.iso'
}

def collect_video_files(root_dir):
    """递归遍历 root_dir 及其所有子目录，返回其中视频文件的完整路径列表。"""
    if not root_dir or not os.path.isdir(root_dir):
        return []
    video_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS:
                video_files.append(os.path.join(dirpath, name))
    video_files.sort(key=str.lower)
    return video_files


_TEXT_FALLBACK_ENCODINGS = ('utf-8-sig', 'gbk')


def _decode_text_bytes(raw):
    """按常见编码解码文本字节，返回 (text, encoding)；无法解码时返回 (None, None)。

    UTF-16 仅在存在 BOM 时尝试，避免把 GBK/UTF-8 字节误判成 UTF-16 乱码。
    """
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return raw.decode('utf-16'), 'utf-16'
        except (UnicodeDecodeError, LookupError):
            return None, None
    for enc in _TEXT_FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None, None


def _is_safe_new_filename(name):
    """检查从 ED2K 链接解析出的文件名是否可安全使用。

    拒绝空名、'.'/'..'、含路径分隔符（含目录穿越风险）、NUL、
    Windows 非法字符以及控制字符；这类链接视为无效链接处理。
    """
    if not name or not isinstance(name, str):
        return False
    if name in ('.', '..'):
        return False
    if '/' in name or '\\' in name or '\x00' in name:
        return False
    if any(c in name for c in '<>:"|?*'):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    return True


def _robust_rename(src, dst):
    """与 os.rename 语义一致，跨盘符（EXDEV）时回退到 shutil.move。"""
    try:
        os.rename(src, dst)
    except OSError as exc:
        if getattr(exc, 'errno', None) != errno.EXDEV:
            raise
        shutil.move(src, dst)


UNMATCHED_DIR_NAME = '@未匹配'


def is_in_unmatched_tree(file_path):
    """判断文件是否位于任意层级的“@未匹配”目录内。"""
    parts = os.path.normpath(os.path.abspath(file_path)).split(os.sep)
    return UNMATCHED_DIR_NAME in parts[:-1]


def move_file_to_unmatched(folder_path, file_path):
    """将未找到匹配链接的文件移动到 folder_path 下的“未匹配”子目录。

    如果文件本身已在“@未匹配”根目录或子目录中，则不再移动，
    避免产生“@未匹配/@未匹配”这样的嵌套结构。
    如果“未匹配”目录不存在则先创建；如果目录内已有重名文件，
    则将新文件重命名为“原文件名（序号）”。
    返回移动后的新路径；如果文件已在“未匹配”目录内则返回 None。
    """
    if is_in_unmatched_tree(file_path):
        return None
    unmatched_dir = os.path.join(folder_path, UNMATCHED_DIR_NAME)
    if not os.path.isdir(unmatched_dir):
        os.makedirs(unmatched_dir, exist_ok=True)

    old_name = os.path.basename(file_path)
    base_name, ext = os.path.splitext(old_name)
    dest_path = os.path.join(unmatched_dir, old_name)
    index = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(unmatched_dir, '{}（{}）{}'.format(base_name, index, ext))
        index += 1
    _robust_rename(file_path, dest_path)
    return dest_path


def move_matched_file_out_of_unmatched(file_path, new_filename):
    """把“@未匹配”目录内已匹配的文件移动到其上一级目录。

    如果上一级目录存在同名文件，则将文件重命名为“原文件名（序号）”后移动。
    返回移动后的完整路径；如果文件不在“@未匹配”目录内则返回 None。
    """
    norm_path = os.path.normpath(os.path.abspath(file_path))
    parts = norm_path.split(os.sep)
    try:
        index = parts.index(UNMATCHED_DIR_NAME)
    except ValueError:
        return None
    if index >= len(parts) - 1:
        return None

    # 从文件路径逐级向上，定位“@未匹配”目录的上一级目录
    parent_dir = norm_path
    for _ in range(len(parts) - index):
        parent_dir = os.path.dirname(parent_dir)

    target_name = new_filename or parts[-1]
    base_name, ext = os.path.splitext(target_name)
    dest_path = os.path.join(parent_dir, target_name)
    counter = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(
            parent_dir, '{}（{}）{}'.format(base_name, counter, ext))
        counter += 1

    _robust_rename(norm_path, dest_path)
    return dest_path


class DropdownPopup(QWidget):
    """按钮下方弹出的下拉选项，宽度、字体和文字位置与触发按钮完全一致。"""

    def __init__(self, owner, text, callback, on_show=None, on_hide=None):
        super().__init__(owner, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.owner = owner
        self.callback = callback
        self.on_show = on_show
        self.on_hide = on_hide

        self.setFixedSize(owner.size())

        self.item_button = QPushButton(text, self)
        self.item_button.setGeometry(0, 0, owner.width(), owner.height())
        self.item_button.setCursor(Qt.PointingHandCursor)
        self.item_button.setFocusPolicy(Qt.NoFocus)
        self.item_button.setStyleSheet(
            "QPushButton{"
            " background-color: #6950a1;"
            " border: none;"
            " border-radius: 10px;"
            " font: bold 10pt;"
            " color: #ffffff;"
            "}"
            "QPushButton::hover{"
            " background-color: #ea66a6;"
            " border-radius: 10px;"
            " font: bold 10pt;"
            " color: #ffffff;"
            "}"
        )
        self.item_button.clicked.connect(self._activate)

    def _activate(self):
        self.close()
        self.callback()

    def popup(self):
        self.item_button.setAttribute(Qt.WA_UnderMouse, False)
        self.item_button.update()
        self.move(self.owner.mapToGlobal(QPoint(0, self.owner.height())))
        self.show()

    def showEvent(self, event):
        super().showEvent(event)
        try:
            if self.on_show:
                self.on_show()
        except RuntimeError:
            pass

    def hideEvent(self, event):
        super().hideEvent(event)
        try:
            self.item_button.setAttribute(Qt.WA_UnderMouse, False)
            self.item_button.update()
        except RuntimeError:
            pass
        try:
            if self.on_hide:
                self.on_hide()
        except RuntimeError:
            pass


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.retranslateUi(self)

        # 设置最小窗口尺寸
        self.setMinimumSize(720, 600)

        self.file_list = []
        self.file_number = 0
        self.folder_path = ''
        self.txt_list = []
        self.txt_paths = []
        self.mode = 1

        # 最大化按钮
        self.maximize_button = QPushButton(self.centralwidget)
        self.maximize_button.setStyleSheet("""
            QPushButton{
                background-color: #6950a1;
                border-radius: 12px;
                font: bold 10pt;
                color: #ffffff;
            }
            QPushButton::hover{
                background-color: #ea66a6;
                border-radius: 12px;
            }
        """)
        self.maximize_button.setText("□")
        self.maximize_button.clicked.connect(self.toggle_maximize)

        # 连接原有按钮
        # “多选文件”按钮：主体直接打开多选文件窗口，右侧下拉箭头选择目录
        self.file_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.file_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.file_menu = QMenu(self)
        self.file_menu.addAction('选择目录', self.select_directory)
        self.file_button.setMenu(self.file_menu)
        self.file_button.clicked.connect(self.select_file)

        # “单选文本”按钮：主体直接打开单选文本窗口，右侧下拉箭头可切换为多选文本
        self.txt_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.txt_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.txt_menu = QMenu(self)
        self.txt_menu.addAction('多选文本', self.select_txt_multiple)
        self.txt_button.setMenu(self.txt_menu)
        self.txt_button.clicked.connect(self.select_txt)

        # 下拉弹层打开时会抓取鼠标，用定时器按光标真实位置刷新主按钮和下拉项的悬停色
        self._popup_hover_timer = QTimer(self)
        self._popup_hover_timer.setInterval(30)
        self._popup_hover_timer.timeout.connect(self._update_buttons_hover_from_cursor)

        # 下拉弹层：宽度、字体和文字对齐与对应按钮完全一致
        self.file_dropdown = DropdownPopup(self.file_button, '选择目录', self.select_directory,
                                           on_show=self._start_popup_hover_tracking,
                                           on_hide=self._on_dropdown_hidden)
        self.txt_dropdown = DropdownPopup(self.txt_button, '多选文本', self.select_txt_multiple,
                                          on_show=self._start_popup_hover_tracking,
                                          on_hide=self._on_dropdown_hidden)
        self._dropdown_press_button = None
        self.file_button.installEventFilter(self)
        self.txt_button.installEventFilter(self)

        self.start_button.clicked.connect(self.start_rename)
        self.minimize.clicked.connect(self.showMinimized)
        self.winclose.clicked.connect(self.close)
        self.moshi.clicked.connect(self.switchmode)

        # 表格设置：最左侧“序号”列 + 三列数据
        self.table.setColumnCount(4)
        self.table.verticalHeader().setVisible(False)
        self.table.setHorizontalHeaderLabels(['序号', '旧文件名', '新文件名', '文件状态'])

        # ★ 核心修改：三列数据 Stretch 平分；序号列固定窄宽，禁止手动拖拽，无任何注册表写入 ★
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 45)
        
        # 滚动条设置（无横向，纵向按需出现）
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # 表头横线、列间竖线都与表格网格线保持一致（1px、#d8d8d8）
        # 先清空表头原生边框，避免与自定义边框叠加后显得又黑又粗
        self.table.horizontalHeader().setStyleSheet(
            "QHeaderView {"
            " background-color: #ffffff;"
            " border: none;"
            " }"
            "QHeaderView::section {"
            " background-color: #ffffff;"
            " color: #130c0e;"
            " border: none;"
            " border-right: 1px solid #d8d8d8;"
            " border-bottom: 1px solid #d8d8d8;"
            " }"
            "QHeaderView::section:last {"
            " border-right: none;"
            " }"
        )

        # 窗口无边框（保留最小化提示，使左键单击任务栏图标可以隐藏/还原窗口）
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.title_label.installEventFilter(self)

        # 鼠标拖动窗口
        self.m_flag = False
        self.m_position = None
        self.title_label.setMouseTracking(True)
        self.title_label.setAcceptDrops(True)

        # 默认高度调整为正好显示 11 行
        self.adjust_default_height()

        # 启动时窗口居于屏幕正中央
        self.center_on_screen()

        # 系统托盘图标：左键单击隐藏/显示窗口
        self.setup_tray_icon()

    def setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.setToolTip('ED2K重命名工具4.0')

        self.tray_menu = QMenu()
        show_action = QAction('显示窗口', self.tray_menu)
        show_action.triggered.connect(self.show_window)
        hide_action = QAction('隐藏窗口', self.tray_menu)
        hide_action.triggered.connect(self.hide)
        quit_action = QAction('退出', self.tray_menu)
        quit_action.triggered.connect(self.quit_app)
        self.tray_menu.addAction(show_action)
        self.tray_menu.addAction(hide_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(self.tray_menu)

        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        # Windows 下左键单击托盘图标为 Trigger
        if reason == QSystemTrayIcon.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.show_window()

    def show_window(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        try:
            work = getattr(self, 'Work', None)
            if work is not None and work.isRunning():
                # 等待工作线程结束，避免 "Destroyed while thread is still running" 崩溃；
                # 超时则请求终止后再等待，确保进程能正常退出。
                if not work.wait(3000):
                    try:
                        work.terminate()
                    except RuntimeError:
                        pass
                    work.wait(3000)
        except RuntimeError:
            pass
        try:
            self.tray_icon.hide()
        except RuntimeError:
            pass
        QApplication.instance().quit()

    def adjust_default_height(self, rows=11):
        """根据表格实际行高调整默认窗口高度，使表格正好显示指定行数。"""
        table = self.table
        row_h = table.verticalHeader().defaultSectionSize()
        # 用 sizeHint 而不是当前 height()：窗口显示前 height() 往往偏大，
        # 会把窗口算高约 6px，导致多露出约 0.2 行。
        header_h = table.horizontalHeader().sizeHint().height()
        frame = table.frameWidth() * 2
        table_h = header_h + row_h * rows + frame
        target_h = 170 + table_h + 10
        if target_h < self.minimumHeight():
            self.setMinimumHeight(target_h)
        self.resize(self.width(), target_h)

    def center_on_screen(self):
        """让窗口在启动时位于屏幕正中央。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        x = rect.x() + (rect.width() - self.width()) // 2
        y = rect.y() + (rect.height() - self.height()) // 2
        self.move(x, y)

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
            self.maximize_button.setText("□")
        else:
            self.showMaximized()
            self.maximize_button.setText("❐")

    def _start_popup_hover_tracking(self):
        self._popup_hover_timer.start()

    def _on_dropdown_hidden(self):
        try:
            if not (self.file_dropdown.isVisible() or self.txt_dropdown.isVisible()):
                self._popup_hover_timer.stop()
        except RuntimeError:
            try:
                self._popup_hover_timer.stop()
            except RuntimeError:
                pass
        try:
            self.refresh_button_hover()
        except RuntimeError:
            pass

    def _update_buttons_hover_from_cursor(self):
        """下拉弹层打开时按其鼠标抓取状态，仍让主按钮和下拉项跟随真实光标显示悬停色。"""
        try:
            if not (self.file_dropdown.isVisible() or self.txt_dropdown.isVisible()):
                self._popup_hover_timer.stop()
                return
            cursor_pos = QCursor.pos()
            for button in (self.file_button, self.txt_button):
                inside = button.rect().contains(button.mapFromGlobal(cursor_pos))
                if button.testAttribute(Qt.WA_UnderMouse) != inside:
                    button.setAttribute(Qt.WA_UnderMouse, inside)
                    button.update()
            for popup in (self.file_dropdown, self.txt_dropdown):
                item = popup.item_button
                inside = popup.isVisible() and item.rect().contains(item.mapFromGlobal(cursor_pos))
                if item.testAttribute(Qt.WA_UnderMouse) != inside:
                    item.setAttribute(Qt.WA_UnderMouse, inside)
                    item.update()
        except RuntimeError:
            # 界面关闭/销毁过程中控件可能已被删除，此时直接停止跟踪即可
            try:
                self._popup_hover_timer.stop()
            except RuntimeError:
                pass

    def refresh_button_hover(self):
        """清除下拉按钮可能残留的悬停状态，避免关闭弹层/对话框后背景卡在粉色。"""
        for button in (self.file_button, self.txt_button):
            try:
                button.setAttribute(Qt.WA_UnderMouse, False)
                button.update()
            except RuntimeError:
                continue

    def _is_in_menu_arrow(self, button, pos):
        """判断鼠标位置是否位于下拉按钮右侧箭头区域。"""
        opt = QStyleOptionToolButton()
        button.initStyleOption(opt)
        rect = button.style().subControlRect(
            QStyle.CC_ToolButton, opt, QStyle.SC_ToolButtonMenu, button)
        return rect.contains(pos)

    def eventFilter(self, obj, event):
        # 下拉箭头区域改用自定义弹层，保证宽度、字体和文字位置与按钮一致
        if obj in (self.file_button, self.txt_button):
            etype = event.type()
            if etype == QEvent.Leave:
                obj.setAttribute(Qt.WA_UnderMouse, False)
                obj.update()
            elif etype == QEvent.Enter:
                obj.setAttribute(Qt.WA_UnderMouse, True)
                obj.update()
            elif etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                if self._is_in_menu_arrow(obj, event.pos()):
                    self._dropdown_press_button = obj
                    return True
            elif etype == QEvent.MouseButtonDblClick:
                if self._is_in_menu_arrow(obj, event.pos()):
                    return True
            elif etype == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._dropdown_press_button is obj:
                    self._dropdown_press_button = None
                    if self._is_in_menu_arrow(obj, event.pos()):
                        if obj is self.file_button:
                            self.file_dropdown.popup()
                        else:
                            self.txt_dropdown.popup()
                        # 弹层打开后交互已转移到弹层，按钮立即恢复紫色
                        self.refresh_button_hover()
                        obj.setAttribute(Qt.WA_UnderMouse, False)
                        obj.update()
                    return True

        if obj == self.title_label and event.type() == QEvent.MouseButtonDblClick:
            self.toggle_maximize()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        
        w = self.centralwidget.width()
        h = self.centralwidget.height()

        # 背景和标题栏全屏适应
        self.background.setGeometry(0, 0, w, h)
        self.title_label.setGeometry(0, 3, w, 32)

        # 右上角控制按钮
        btn_size = 24
        spacing = 4
        y = 5
        right_x = w - 10
        self.winclose.setGeometry(right_x - btn_size, y, btn_size, btn_size)
        self.maximize_button.setGeometry(right_x - 2*btn_size - spacing, y, btn_size, btn_size)
        self.minimize.setGeometry(right_x - 3*btn_size - 2*spacing, y, btn_size, btn_size)
        self.ico_label.setGeometry(10, 6, 25, 25)

        # 定义统一的布局参数，确保上下完全对称 (高度30，行间距40)
        row_h = 30
        btn_w = 105
        btn_x = w - 10 - btn_w

        # 第一行 (Y=50)
        self.file_button.setGeometry(btn_x, 50, btn_w, row_h)
        self.file_label.setGeometry(20, 50, 140, row_h)
        self.file_line_edit.setGeometry(140, 50, w - 140 - btn_w - 30, row_h)

        # 第二行 (Y=90)
        self.txt_button.setGeometry(btn_x, 90, btn_w, row_h)
        self.txt_label.setGeometry(20, 90, 140, row_h)
        self.txt_line_edit.setGeometry(140, 90, w - 140 - btn_w - 30, row_h)

        # 第三行 (Y=130)
        mode_btn_w = 105
        progress_w = w - 20 - btn_w - mode_btn_w - 30
        progress_w = max(progress_w, 100)
        
        self.pgb.setGeometry(20, 130, progress_w, row_h)
        self.moshi.setGeometry(20 + progress_w + 10, 130, mode_btn_w, row_h)
        self.start_button.setGeometry(btn_x, 130, btn_w, row_h)

        # 表格随窗口同步拉伸（列宽已由 QHeaderView.Stretch 自动平分，无需多余代码）
        table_x = 10
        table_y = 170
        table_w = w - 20
        table_h = h - table_y - 10

        if table_w > 0 and table_h > 0:
            self.table.setGeometry(table_x, table_y, table_w, table_h)

    def switchmode(self):
        if self.mode == 1:
            self.mode = 0
            self.moshi.setText('精准模式')
            self.moshi.setStyleSheet('''QPushButton{
                background-color: #ffc20e;
                border-radius: 10px;
                font: bold 10pt;
                color: #ffffff;
            }
            QPushButton::hover{
                background-color: #ea66a6;
                border-radius: 10px;
                font: bold 10pt;
                color: #ffffff;
            }''')
        else:
            self.mode = 1
            self.moshi.setText('极速模式')
            self.moshi.setStyleSheet('''QPushButton{
                background-color: #7fb80e;
                border-radius: 10px;
                font: bold 10pt;
                color: #ffffff;
            }
            QPushButton::hover{
                background-color: #ea66a6;
                border-radius: 10px;
                font: bold 10pt;
                color: #ffffff;
            }''')

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_button_hover()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.childAt(event.pos()) == self.title_label:
            self.m_flag = True
            self.m_position = event.globalPos() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.m_flag:
            self.move(event.globalPos() - self.m_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.m_flag = False

    def set_file_list(self, files, folder_path, source):
        """统一设置待处理文件列表并刷新表格。

        source 为 'file' 表示多选文件，'dir' 表示选择目录递归遍历。
        """
        if not files:
            return
        self.file_list = files
        self.folder_path = folder_path
        self.file_number = len(files)

        if source == 'dir':
            folder_name = os.path.basename(os.path.normpath(folder_path))
            self.file_line_edit.setText(
                ' 已选择目录<<<' + folder_name + '>>>及子目录,共[' +
                str(self.file_number) + ']个视频文件'
            )
        else:
            editfile_name = os.path.basename(files[0])
            if len(editfile_name) > 20:
                editfile_name = editfile_name[:40] + '...'
            self.file_line_edit.setText(
                ' 已选择' + editfile_name + '等共[' + str(self.file_number) + ']个文件'
            )

        self.table.setRowCount(self.file_number)
        for i, file_path in enumerate(files):
            file_name = os.path.basename(file_path)
            serial_item = QTableWidgetItem(str(i + 1))
            serial_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, serial_item)
            self.table.setItem(i, 1, QTableWidgetItem(file_name))
            self.table.setItem(i, 2, QTableWidgetItem(''))
            self.table.setItem(i, 3, QTableWidgetItem('等待处理'))
        self.table.verticalScrollBar().setSliderPosition(0)
        self.pgb.setValue(0)

    def select_file(self):
        file_dialog = QFileDialog()
        file_dialog.setFileMode(QFileDialog.ExistingFiles)
        file_dialog.setNameFilter('All Files (*.*)')
        if file_dialog.exec_():
            files = file_dialog.selectedFiles()
            if files:
                self.set_file_list(files, os.path.dirname(files[0]), 'file')
        self.refresh_button_hover()

    def select_directory(self):
        """选择一个目录，递归遍历根目录及子目录下的所有视频文件。"""
        folder = QFileDialog.getExistingDirectory(self, '选择目录')
        self.refresh_button_hover()
        if not folder:
            return
        files = collect_video_files(folder)
        if not files:
            QMessageBox.information(self, '提示', '所选目录及其子目录下没有找到视频文件')
            self.refresh_button_hover()
            return
        self.set_file_list(files, folder, 'dir')
        self.refresh_button_hover()

    def select_txt(self):
        """单选文本：只能选择一个文本文件。"""
        fname, _ = QFileDialog.getOpenFileName(self, '选择文件', '', 'Text files (*.txt)')
        self.refresh_button_hover()
        if fname:
            self.load_txt_files([fname])

    def select_txt_multiple(self):
        """多选文本：可同时选择多个文本文件。"""
        fnames, _ = QFileDialog.getOpenFileNames(self, '选择文件', '', 'Text files (*.txt)')
        self.refresh_button_hover()
        if fnames:
            self.load_txt_files(fnames)

    def load_txt_files(self, fnames):
        if not fnames:
            return
        self.txt_paths = list(fnames)
        ed2k_links = []
        seen = set()
        for txt_path in self.txt_paths:
            try:
                with open(txt_path, 'rb') as f:
                    raw = f.read()
            except OSError:
                continue
            text, _ = _decode_text_bytes(raw)
            if text is None:
                continue
            for line in text.splitlines():
                if 'ed2k://|file|' in line:
                    link = line.strip()
                    if link and link not in seen:
                        seen.add(link)
                        ed2k_links.append(link)

        self.txt_list = ed2k_links
        count = len(ed2k_links)

        first_name = os.path.basename(self.txt_paths[0])
        if len(first_name) > 20:
            first_name = first_name[:25] + '...'

        if len(self.txt_paths) == 1:
            self.txt_line_edit.setText(
                ' 已选择<<<' + first_name + '>>>,共找到[' + str(count) + ']个ED2K链接'
            )
        else:
            self.txt_line_edit.setText(
                ' 已选择<<<' + first_name + '>>>等共[' + str(len(self.txt_paths)) +
                ']个文本,共找到[' + str(count) + ']个ED2K链接'
            )


    def start_rename(self):
        if not self.folder_path:
            QMessageBox.warning(self, '警告', '请选择需要重命名的文件')
            return
        if not self.txt_paths:
            QMessageBox.warning(self, '警告', '请选择ED2K文本文档')
            return
        if not self.txt_list:
            QMessageBox.warning(self, '警告', '已选择的txt文档内没有任何ED2K链接，请重新选择')
            return

        work = getattr(self, 'Work', None)
        if work is not None:
            try:
                if work.isRunning():
                    return
            except RuntimeError:
                pass

        for i in range(self.file_number):
            self.table.setItem(i, 2, QTableWidgetItem(''))
            self.table.setItem(i, 3, QTableWidgetItem('等待处理'))
        self.table.verticalScrollBar().setSliderPosition(0)
        self.pgb.setValue(0)

        self.file_button.setEnabled(False)
        self.txt_button.setEnabled(False)
        self.start_button.setEnabled(False)

        if self.mode == 1:
            if self.file_number > 0:
                self.table.setItem(0, 3, QTableWidgetItem('处理中'))
            self.Work = ButtonThread2()
            self.Work.updateui_signal.connect(self.update_table)
            self.Work.finish_signal.connect(self.rewrite_txt)
            self.Work.inputset(self.folder_path, self.file_list, self.txt_list)
            self.Work.start()
        else:
            if self.file_number > 0:
                self.table.setItem(0, 3, QTableWidgetItem('处理中'))
            self.Work = ButtonThread1()
            self.Work.updateui_signal.connect(self.update_table)
            self.Work.finish_signal.connect(self.rewrite_txt)
            self.Work.inputset(self.folder_path, self.file_list, self.txt_list)
            self.Work.start()

    def update_table(self, row, new_filename, state, color):
        if row < 0 or row >= self.table.rowCount():
            return
        self.table.setItem(row, 2, QTableWidgetItem(new_filename))
        self.table.setItem(row, 3, QTableWidgetItem(state))
        if row + 1 < self.table.rowCount():
            self.table.setItem(row + 1, 3, QTableWidgetItem('处理中'))
        if row > 7:
            self.table.verticalScrollBar().setSliderPosition(row - 6)

        itemColor = self.table.item(row, 3)
        if itemColor is not None:
            if color == 1:
                itemColor.setBackground(QColor('#77787b'))
            elif color == 2:
                itemColor.setBackground(QColor('#ed1941'))
            elif color == 3:
                itemColor.setBackground(QColor('#7fb80e'))

        if self.file_number > 0:
            Value = int(((row + 1) / self.file_number) * 100)
        else:
            Value = 100
        if Value == 100:
            self.file_button.setEnabled(True)
            self.txt_button.setEnabled(True)
            self.start_button.setEnabled(True)
            self.refresh_button_hover()
        self.pgb.setValue(Value)

    def rewrite_txt(self, matched_links):
        """对每个已选择的文本文件分别执行整理。"""
        try:
            for txt_path in list(self.txt_paths):
                try:
                    self.rewrite_single_txt(txt_path, matched_links)
                except Exception:
                    continue
        finally:
            try:
                self.file_button.setEnabled(True)
                self.txt_button.setEnabled(True)
                self.start_button.setEnabled(True)
                self.refresh_button_hover()
            except RuntimeError:
                pass

    def rewrite_single_txt(self, txt_path, matched_links):
        """未匹配链接放到文本末尾；上次未匹配、这次已匹配的链接移回已匹配区域，
        并在发生这种移动时把已匹配区域的链接按升序排列。"""
        matched_set = set(matched_links or [])
        try:
            with open(txt_path, 'rb') as f:
                raw = f.read()
        except OSError:
            return
        text, encoding = _decode_text_bytes(raw)
        if text is None:
            return

        # 保持文本原有的换行风格
        newline = '\r\n' if '\r\n' in text else '\n'
        original_lines = text.splitlines()

        def is_link(line):
            return 'ed2k://|file|' in line

        # 找出末尾“未匹配链接区域”：末尾连续的非空链接块，
        # 且它前面紧挨着一行空行（这是上一次整理后留下的分隔）。
        end = len(original_lines)
        while end > 0 and not original_lines[end - 1].strip():
            end -= 1

        block_start = end
        while block_start > 0 and is_link(original_lines[block_start - 1]):
            block_start -= 1

        has_unmatched_area = (
            block_start < end
            and block_start > 0
            and not original_lines[block_start - 1].strip()
        )

        if has_unmatched_area:
            prefix_lines = original_lines[:block_start - 1]
            unmatched_area_lines = original_lines[block_start:end]
        else:
            prefix_lines = original_lines[:end]
            unmatched_area_lines = []

        # 未匹配区域中这次已经匹配上的链接
        moved_links = []
        remaining_unmatched = []
        for line in unmatched_area_lines:
            if is_link(line):
                link = line.strip()
                if link in matched_set:
                    moved_links.append(link)
                else:
                    remaining_unmatched.append(link)

        if moved_links:
            # 收集已匹配区域里的链接行
            prefix_link_indexes = []
            prefix_matched = []
            prefix_unmatched = []
            for idx, line in enumerate(prefix_lines):
                if is_link(line):
                    prefix_link_indexes.append(idx)
                    link = line.strip()
                    if link in matched_set:
                        prefix_matched.append(link)
                    else:
                        prefix_unmatched.append(link)

            # 有链接从未匹配区域移上来：已匹配区域的链接统一按升序排列
            sorted_matched = sorted(prefix_matched + moved_links, key=str.lower)

            # 去掉已匹配区域里的所有链接行，再把排序后的链接放回第一个链接的位置；
            # 非链接行保持原来的先后顺序。
            link_index_set = set(prefix_link_indexes)
            prefix_without_links = [
                line for idx, line in enumerate(prefix_lines)
                if idx not in link_index_set
            ]
            while prefix_without_links and not prefix_without_links[-1].strip():
                prefix_without_links.pop()

            if prefix_link_indexes:
                insert_at = min(prefix_link_indexes[0], len(prefix_without_links))
            else:
                insert_at = len(prefix_without_links)

            kept_lines = (
                prefix_without_links[:insert_at]
                + sorted_matched
                + prefix_without_links[insert_at:]
            )

            # 已匹配区域里原本未匹配的链接也一起移到文本末尾
            remaining_unmatched = prefix_unmatched + remaining_unmatched
        else:
            # 没有链接从“未匹配区域”移到“已匹配区域”：保持原来的整理逻辑
            kept_lines = []
            remaining_unmatched = []
            for line in original_lines:
                if is_link(line):
                    link = line.strip()
                    if link in matched_set:
                        kept_lines.append(link)
                    else:
                        remaining_unmatched.append(link)
                else:
                    kept_lines.append(line.rstrip())

        # 去掉末尾多余的空行，确保与未匹配链接之间只间隔一行空行
        while kept_lines and not kept_lines[-1].strip():
            kept_lines.pop()

        new_lines = list(kept_lines)
        if kept_lines and remaining_unmatched:
            new_lines.append('')
        new_lines.extend(remaining_unmatched)

        try:
            write_encoding = 'utf-8' if encoding == 'utf-8-sig' else encoding
            with open(txt_path, 'w', encoding=write_encoding, newline='') as f:
                f.write(newline.join(new_lines) + (newline if new_lines else ''))
        except OSError:
            pass


class ButtonThread1(QThread):
    updateui_signal = pyqtSignal(int, str, str, int)
    finish_signal = pyqtSignal(list)

    def __init__(self):
        super(ButtonThread1, self).__init__()

    def inputset(self, folder_path, file_list, txt_list):
        self.folder_path = folder_path
        self.file_list = file_list
        self.txt_list = txt_list

    def md4(self, x):
        h = MD4.new()
        h.update(x)
        return h

    def ed2k(self, file_path):
        ed2k_block = 9500 * 1024
        ed2k_hash = b''
        with open(file_path, 'rb') as f:
            file_size = os.fstat(f.fileno()).st_size
            if file_size == 0:
                return None
            while True:
                block = f.read(ed2k_block)
                if not block:
                    break
                ed2k_hash += self.md4(block).digest()
            if file_size % ed2k_block == 0:
                ed2k_hash += self.md4(b'').digest()
        ed2k_hash = self.md4(ed2k_hash).hexdigest()
        return ed2k_hash.upper()

    def search_ed2k_link(self, ed2k_txt, ed2k_hash):
        target = ed2k_hash.upper()
        return [line for line in ed2k_txt if target in line.upper()]

    def run(self):
        matched_links = []
        matched_set = set()
        file_list = list(self.file_list or [])
        txt_list = list(self.txt_list or [])
        folder_path = self.folder_path
        try:
            for i, item in enumerate(file_list):
                try:
                    if os.path.exists(item):
                        old_filepath = item
                        old_filename = os.path.basename(item)
                        try:
                            ed2k_hash = self.ed2k(old_filepath)
                        except OSError:
                            self.updateui_signal.emit(i, '', '读取文件失败，跳过', 2)
                            continue
                        if ed2k_hash is None:
                            self.updateui_signal.emit(i, '', '文件大小为0，跳过', 1)
                            continue
                        ed2k_links = self.search_ed2k_link(txt_list, ed2k_hash)
                        for link in ed2k_links:
                            if link not in matched_set:
                                matched_set.add(link)
                                matched_links.append(link)
                        if not ed2k_links:
                            try:
                                moved = move_file_to_unmatched(folder_path, old_filepath)
                                if moved:
                                    self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，已移动到未匹配', 1)
                                else:
                                    self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，跳过', 1)
                            except OSError:
                                self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，移动到未匹配失败', 2)
                            continue
                        in_unmatched = is_in_unmatched_tree(old_filepath)
                        renamed = False
                        for k, link in enumerate(ed2k_links):
                            parts = link.split('|')
                            if len(parts) < 5:
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, '', '无效的ed2k链接', 2)
                                continue
                            new_filename = parts[2]
                            if not _is_safe_new_filename(new_filename):
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, '', '无效的ed2k链接', 2)
                                continue
                            if new_filename == old_filename:
                                if in_unmatched:
                                    try:
                                        moved_path = move_matched_file_out_of_unmatched(
                                            old_filepath, new_filename)
                                        if not moved_path:
                                            raise OSError('move out failed')
                                    except OSError:
                                        self.updateui_signal.emit(
                                            i, new_filename, '新旧文件名一致，文件移回上一级目录失败', 2)
                                    else:
                                        self.updateui_signal.emit(
                                            i, os.path.basename(moved_path),
                                            '新旧文件名一致，已将文件移回上一级目录', 3)
                                else:
                                    self.updateui_signal.emit(i, new_filename, '新旧文件名一致，跳过', 1)
                                renamed = True
                                break
                            if in_unmatched:
                                try:
                                    moved_path = move_matched_file_out_of_unmatched(
                                        old_filepath, new_filename)
                                    if not moved_path:
                                        raise OSError('move out failed')
                                except OSError:
                                    if k == len(ed2k_links) - 1:
                                        self.updateui_signal.emit(i, new_filename, '文件重命名失败', 2)
                                    continue
                                self.updateui_signal.emit(
                                    i, os.path.basename(moved_path),
                                    '文件重命名成功，已将文件移回上一级目录', 3)
                                renamed = True
                                break
                            new_filepath = os.path.join(os.path.dirname(old_filepath), new_filename)
                            if os.path.exists(new_filepath) and new_filename.lower() != old_filename.lower():
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, new_filename, '重命名失败，已存在相同名称的文件', 2)
                                continue
                            try:
                                _robust_rename(old_filepath, new_filepath)
                            except OSError:
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, new_filename, '文件重命名失败', 2)
                                continue
                            self.updateui_signal.emit(i, new_filename, '重命名成功', 3)
                            renamed = True
                            break
                        if not renamed:
                            self.updateui_signal.emit(i, '', '未找到可用链接', 2)
                    else:
                        self.updateui_signal.emit(i, '', '文件不存在，请检查文件是否已删除', 2)
                except Exception:
                    try:
                        self.updateui_signal.emit(i, '', '读取文件失败，跳过', 2)
                    except Exception:
                        pass
        finally:
            try:
                self.finish_signal.emit(matched_links)
            except Exception:
                pass


class ButtonThread2(QThread):
    updateui_signal = pyqtSignal(int, str, str, int)
    finish_signal = pyqtSignal(list)

    def __init__(self):
        super(ButtonThread2, self).__init__()

    def inputset(self, folder_path, file_list, txt_list):
        self.folder_path = folder_path
        self.file_list = file_list
        self.txt_list = txt_list

    def ed2k(self, file_path):
        with open(file_path, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            return str(size) if size > 0 else None

    def search_ed2k_link(self, ed2k_txt, file_size):
        matched = []
        for line in ed2k_txt:
            parts = line.split('|')
            if len(parts) > 3 and parts[3].strip() == file_size:
                matched.append(line)
        return matched

    def run(self):
        matched_links = []
        matched_set = set()
        file_list = list(self.file_list or [])
        txt_list = list(self.txt_list or [])
        folder_path = self.folder_path
        try:
            for i, item in enumerate(file_list):
                try:
                    if os.path.exists(item):
                        old_filepath = item
                        old_filename = os.path.basename(item)
                        try:
                            file_size = self.ed2k(old_filepath)
                        except OSError:
                            self.updateui_signal.emit(i, '', '读取文件失败,跳过', 2)
                            continue
                        if file_size is None:
                            self.updateui_signal.emit(i, '', '文件大小为0,跳过', 1)
                            continue
                        ed2k_links = self.search_ed2k_link(txt_list, file_size)
                        for link in ed2k_links:
                            if link not in matched_set:
                                matched_set.add(link)
                                matched_links.append(link)
                        if not ed2k_links:
                            try:
                                moved = move_file_to_unmatched(folder_path, old_filepath)
                                if moved:
                                    self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，已移动到未匹配', 1)
                                else:
                                    self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，跳过', 1)
                            except OSError:
                                self.updateui_signal.emit(i, '', '没有匹配的ED2K链接，移动到未匹配失败', 2)
                            continue
                        in_unmatched = is_in_unmatched_tree(old_filepath)
                        renamed = False
                        for k, link in enumerate(ed2k_links):
                            parts = link.split('|')
                            if len(parts) < 5:
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, '', '无效的ed2k链接', 2)
                                continue
                            new_filename = parts[2]
                            if not _is_safe_new_filename(new_filename):
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, '', '无效的ed2k链接', 2)
                                continue
                            if new_filename == old_filename:
                                if in_unmatched:
                                    try:
                                        moved_path = move_matched_file_out_of_unmatched(
                                            old_filepath, new_filename)
                                        if not moved_path:
                                            raise OSError('move out failed')
                                    except OSError:
                                        self.updateui_signal.emit(
                                            i, new_filename, '新旧文件名一致，文件移回上一级目录失败', 2)
                                    else:
                                        self.updateui_signal.emit(
                                            i, os.path.basename(moved_path),
                                            '新旧文件名一致，已将文件移回上一级目录', 3)
                                else:
                                    self.updateui_signal.emit(i, new_filename, '新旧文件名一致，跳过', 1)
                                renamed = True
                                break
                            if in_unmatched:
                                try:
                                    moved_path = move_matched_file_out_of_unmatched(
                                        old_filepath, new_filename)
                                    if not moved_path:
                                        raise OSError('move out failed')
                                except OSError:
                                    if k == len(ed2k_links) - 1:
                                        self.updateui_signal.emit(i, new_filename, '文件重命名失败', 2)
                                    continue
                                self.updateui_signal.emit(
                                    i, os.path.basename(moved_path),
                                    '文件重命名成功，已将文件移回上一级目录', 3)
                                renamed = True
                                break
                            new_filepath = os.path.join(os.path.dirname(old_filepath), new_filename)
                            if os.path.exists(new_filepath) and new_filename.lower() != old_filename.lower():
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, new_filename, '重命名失败，已存在相同名称的文件', 2)
                                continue
                            try:
                                _robust_rename(old_filepath, new_filepath)
                            except OSError:
                                if k == len(ed2k_links) - 1:
                                    self.updateui_signal.emit(i, new_filename, '文件重命名失败', 2)
                                continue
                            self.updateui_signal.emit(i, new_filename, '重命名成功', 3)
                            renamed = True
                            break
                        if not renamed:
                            self.updateui_signal.emit(i, '', '未找到可用链接', 2)
                    else:
                        self.updateui_signal.emit(i, '', '文件不存在，请检查文件是否已删除', 2)
                except Exception:
                    try:
                        self.updateui_signal.emit(i, '', '读取文件失败,跳过', 2)
                    except Exception:
                        pass
        finally:
            try:
                self.finish_signal.emit(matched_links)
            except Exception:
                pass


if __name__ == '__main__':
    # 适配高DPI屏幕
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())