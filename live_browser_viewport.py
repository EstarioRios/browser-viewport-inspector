#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live Browser Viewport
--------------------
A small Chromium-based browser embedded in a PySide6 desktop window.

Features
- Enter a URL and open it with the Run button.
- Set the web page's viewport width/height in CSS pixels.
- Width/height changes are applied live after a short typing pause;
  the spin-box arrows apply immediately.
- Live mouse coordinates are read from the actual web page (clientX/clientY).
- A small, click-through coordinate badge is displayed over the browser view.
- Both viewport coordinates and document/page coordinates are shown.
- The browser view itself keeps the requested dimensions. If it is larger
  than the desktop window, the surrounding scroll area lets you inspect it.

Install on Windows:
    py -3 -m pip install -r requirements.txt

Run:
    py -3 live_browser_viewport.py
    py -3 live_browser_viewport.py --width 1280 --height 800
    py -3 live_browser_viewport.py --url https://example.com --width 1365 --height 768
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QIntValidator, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView


MIN_VIEWPORT_SIZE = 100
MAX_VIEWPORT_SIZE = 12000


class CoordinateBridge(QObject):
    """Methods exposed to JavaScript by QWebChannel."""

    pointerMoved = Signal(float, float, float, float, float, float, float, float, float)
    pointerLeft = Signal()
    clicked = Signal(float, float, float, float)
    viewportChanged = Signal(float, float, float)

    @Slot(float, float, float, float, float, float, float, float, float)
    def reportPointer(
        self,
        client_x: float,
        client_y: float,
        page_x: float,
        page_y: float,
        viewport_width: float,
        viewport_height: float,
        scroll_x: float,
        scroll_y: float,
        device_pixel_ratio: float,
    ) -> None:
        self.pointerMoved.emit(
            client_x,
            client_y,
            page_x,
            page_y,
            viewport_width,
            viewport_height,
            scroll_x,
            scroll_y,
            device_pixel_ratio,
        )

    @Slot()
    def reportPointerLeft(self) -> None:
        self.pointerLeft.emit()

    @Slot(float, float, float, float)
    def reportClick(self, client_x: float, client_y: float, page_x: float, page_y: float) -> None:
        self.clicked.emit(client_x, client_y, page_x, page_y)

    @Slot(float, float, float)
    def reportViewport(self, width: float, height: float, device_pixel_ratio: float) -> None:
        self.viewportChanged.emit(width, height, device_pixel_ratio)


class LiveDimensionSpinBox(QSpinBox):
    """Spin box that applies typed values after a short pause, not just on Enter."""

    liveValueChanged = Signal(int)

    def __init__(self, initial_value: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setRange(MIN_VIEWPORT_SIZE, MAX_VIEWPORT_SIZE)
        self.setValue(initial_value)
        self.setKeyboardTracking(False)
        self.setAccelerated(True)
        self.setButtonSymbols(QSpinBox.ButtonSymbols.UpDownArrows)
        self.setFixedWidth(112)

        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(350)
        self._typing_timer.timeout.connect(self._apply_typed_value)

        self.lineEdit().setValidator(QIntValidator(MIN_VIEWPORT_SIZE, MAX_VIEWPORT_SIZE, self))
        self.lineEdit().textEdited.connect(lambda _text: self._typing_timer.start())
        self.valueChanged.connect(self.liveValueChanged.emit)

    def _apply_typed_value(self) -> None:
        text = self.lineEdit().text().strip()
        if not text or not text.isdecimal():
            return
        value = int(text)
        if MIN_VIEWPORT_SIZE <= value <= MAX_VIEWPORT_SIZE and value != self.value():
            self.setValue(value)


class OverlayScrollArea(QScrollArea):
    """Scroll area that notifies the window when its visible viewport resizes."""

    viewportResized = Signal()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.viewportResized.emit()


class LiveBrowserWindow(QMainWindow):
    def __init__(
        self,
        width: int = 1280,
        height: int = 800,
        initial_url: str = "",
        title: str = "Live Browser Viewport",
    ) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.setMinimumSize(850, 520)

        self.target_width = width
        self.target_height = height
        self.last_pointer: Optional[tuple[float, float, float, float]] = None
        self.last_click: Optional[tuple[float, float, float, float]] = None
        self.actual_viewport = (0, 0, 1.0)

        self._build_ui()
        self._configure_browser()
        self._connect_signals()
        self._apply_viewport_size(width, height, announce=False)
        self._position_coordinate_overlay()
        self._install_shortcuts()

        if initial_url:
            self.url_edit.setText(initial_url)
            QTimer.singleShot(250, self.open_url)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 8)
        root_layout.setSpacing(7)

        # Navigation and URL row
        nav_row = QHBoxLayout()
        nav_row.setSpacing(5)

        self.back_button = QPushButton("←")
        self.back_button.setToolTip("Back")
        self.back_button.setFixedWidth(38)
        self.forward_button = QPushButton("→")
        self.forward_button.setToolTip("Forward")
        self.forward_button.setFixedWidth(38)
        self.reload_button = QPushButton("↻")
        self.reload_button.setToolTip("Reload")
        self.reload_button.setFixedWidth(38)
        self.stop_button = QPushButton("✕")
        self.stop_button.setToolTip("Stop loading")
        self.stop_button.setFixedWidth(38)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com  یا  http://localhost:8000")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setMinimumWidth(250)

        self.run_button = QPushButton("اجرا / باز کردن")
        self.run_button.setDefault(True)
        self.run_button.setMinimumWidth(116)

        nav_row.addWidget(self.back_button)
        nav_row.addWidget(self.forward_button)
        nav_row.addWidget(self.reload_button)
        nav_row.addWidget(self.stop_button)
        nav_row.addWidget(self.url_edit, 1)
        nav_row.addWidget(self.run_button)
        root_layout.addLayout(nav_row)

        # Exact viewport dimensions row
        dimension_row = QHBoxLayout()
        dimension_row.setSpacing(8)

        width_label = QLabel("عرض صفحه (CSS px):")
        self.width_spin = LiveDimensionSpinBox(self.target_width)
        height_label = QLabel("ارتفاع صفحه (CSS px):")
        self.height_spin = LiveDimensionSpinBox(self.target_height)

        self.target_size_label = QLabel()
        self.target_size_label.setObjectName("targetSizeLabel")

        self.overlay_checkbox = QCheckBox("نمایش مختصات روی مرورگر")
        self.overlay_checkbox.setChecked(True)

        dimension_row.addWidget(width_label)
        dimension_row.addWidget(self.width_spin)
        dimension_row.addSpacing(8)
        dimension_row.addWidget(height_label)
        dimension_row.addWidget(self.height_spin)
        dimension_row.addSpacing(10)
        dimension_row.addWidget(self.target_size_label)
        dimension_row.addStretch(1)
        dimension_row.addWidget(self.overlay_checkbox)
        root_layout.addLayout(dimension_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("separator")
        root_layout.addWidget(separator)

        # The browser widget retains the requested size. The outer scroll area
        # only clips it when the requested viewport is larger than the window.
        self.browser_scroll = OverlayScrollArea()
        self.browser_scroll.setWidgetResizable(False)
        self.browser_scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.browser_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.browser_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.browser = QWebEngineView()
        self.browser.setMouseTracking(True)
        self.browser.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.browser.setZoomFactor(1.0)
        self.browser_scroll.setWidget(self.browser)
        root_layout.addWidget(self.browser_scroll, 1)

        # This label is a real Qt overlay above the web view. It does not
        # intercept pointer movement/clicks on the website.
        self.coordinate_overlay = QLabel("X: —    Y: —", self.browser_scroll.viewport())
        self.coordinate_overlay.setObjectName("coordinateOverlay")
        self.coordinate_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.coordinate_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.coordinate_overlay.setMargin(7)
        self.coordinate_overlay.adjustSize()
        self.coordinate_overlay.raise_()

        # Live coordinate / viewport status area
        status_frame = QFrame()
        status_frame.setObjectName("statusFrame")
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(10, 6, 10, 6)
        status_layout.setSpacing(3)

        self.coordinate_label = QLabel("مختصات موس: X: —    Y: —")
        self.coordinate_label.setObjectName("coordinateLabel")
        self.coordinate_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.detail_label = QLabel("مختصات صفحه: X: —    Y: —")
        self.detail_label.setObjectName("detailLabel")
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.viewport_label = QLabel("Viewport واقعی: در انتظار بارگذاری صفحه…")
        self.viewport_label.setObjectName("viewportLabel")

        self.status_label = QLabel("آدرس را وارد کن و روی «اجرا / باز کردن» بزن.")
        self.status_label.setObjectName("statusLabel")

        status_layout.addWidget(self.coordinate_label)
        status_layout.addWidget(self.detail_label)
        status_layout.addWidget(self.viewport_label)
        status_layout.addWidget(self.status_label)
        root_layout.addWidget(status_frame)

        self.setCentralWidget(central)
        self._apply_styles()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #111827; color: #e5e7eb; }
            QLabel { color: #e5e7eb; }
            QLineEdit, QSpinBox {
                background: #1f2937;
                color: #f9fafb;
                border: 1px solid #374151;
                border-radius: 6px;
                padding: 6px 8px;
                selection-background-color: #2563eb;
            }
            QPushButton {
                background: #263449;
                color: #f9fafb;
                border: 1px solid #43516a;
                border-radius: 6px;
                padding: 7px 10px;
            }
            QPushButton:hover { background: #334967; }
            QPushButton:pressed { background: #1d4ed8; }
            QPushButton:default { background: #1d4ed8; border-color: #3b82f6; }
            QCheckBox { spacing: 7px; }
            QScrollArea { background: #0b1220; border: 1px solid #334155; }
            #separator { color: #334155; }
            #targetSizeLabel { color: #93c5fd; font-weight: 600; }
            #statusFrame { background: #172033; border: 1px solid #2d3b52; border-radius: 7px; }
            #coordinateLabel { color: #7dd3fc; font: 700 12pt 'Consolas'; }
            #detailLabel { color: #cbd5e1; font: 10pt 'Consolas'; }
            #viewportLabel { color: #a7f3d0; font: 9pt 'Consolas'; }
            #statusLabel { color: #94a3b8; font-size: 9pt; }
            #coordinateOverlay {
                color: #ffffff;
                background: rgba(10, 15, 25, 220);
                border: 1px solid #38bdf8;
                border-radius: 7px;
                font: 700 11pt 'Consolas';
            }
            QScrollBar:vertical, QScrollBar:horizontal {
                background: #111827; width: 13px; height: 13px; margin: 0;
            }
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
                background: #475569; border-radius: 5px; min-height: 25px; min-width: 25px;
            }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
            """
        )

    # ------------------------------------------------------------ Browser
    def _configure_browser(self) -> None:
        page = self.browser.page()
        page.setBackgroundColor(QColor("#ffffff"))

        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)

        self.bridge = CoordinateBridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("viewportBridge", self.bridge)
        page.setWebChannel(self.channel, QWebEngineScript.ScriptWorldId.ApplicationWorld)

        script = QWebEngineScript()
        script.setName("live-browser-viewport-coordinates")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(self._make_coordinate_script())
        page.scripts().insert(script)

        # Keep the default profile so normal cookies/cache persist between runs.
        profile = page.profile()
        profile.downloadRequested.connect(self._handle_download)

    def _make_coordinate_script(self) -> str:
        """Build a page script using Qt's bundled qwebchannel.js resource."""
        qwebchannel_file = QFile(":/qtwebchannel/qwebchannel.js")
        if not qwebchannel_file.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError(
                "فایل داخلی qwebchannel.js پیدا نشد. نصب PySide6 را با دستور requirements.txt تعمیر کن."
            )
        qwebchannel_js = bytes(qwebchannel_file.readAll()).decode("utf-8")
        qwebchannel_file.close()

        listener_js = r"""
        (function () {
            if (window.__liveBrowserViewportStarted) return;
            window.__liveBrowserViewportStarted = true;

            function connectBridge(attempt) {
                if (typeof QWebChannel === "undefined" || !window.qt || !qt.webChannelTransport) {
                    if (attempt < 40) window.setTimeout(function () { connectBridge(attempt + 1); }, 50);
                    return;
                }

                new QWebChannel(qt.webChannelTransport, function (channel) {
                    var bridge = channel.objects.viewportBridge;
                    if (!bridge) return;

                    var pendingFrame = false;
                    var latest = null;

                    function sendLatestPointer() {
                        pendingFrame = false;
                        if (!latest) return;
                        bridge.reportPointer(
                            latest.clientX,
                            latest.clientY,
                            latest.pageX,
                            latest.pageY,
                            window.innerWidth,
                            window.innerHeight,
                            window.scrollX,
                            window.scrollY,
                            window.devicePixelRatio || 1
                        );
                    }

                    document.addEventListener("mousemove", function (event) {
                        latest = {
                            clientX: Number(event.clientX),
                            clientY: Number(event.clientY),
                            pageX: Number(event.pageX),
                            pageY: Number(event.pageY)
                        };
                        if (!pendingFrame) {
                            pendingFrame = true;
                            window.requestAnimationFrame(sendLatestPointer);
                        }
                    }, true);

                    document.addEventListener("click", function (event) {
                        bridge.reportClick(
                            Number(event.clientX), Number(event.clientY),
                            Number(event.pageX), Number(event.pageY)
                        );
                    }, true);

                    document.addEventListener("mouseout", function (event) {
                        if (!event.relatedTarget && !event.toElement) {
                            bridge.reportPointerLeft();
                        }
                    }, true);

                    window.addEventListener("resize", function () {
                        bridge.reportViewport(
                            window.innerWidth,
                            window.innerHeight,
                            window.devicePixelRatio || 1
                        );
                    });

                    bridge.reportViewport(
                        window.innerWidth,
                        window.innerHeight,
                        window.devicePixelRatio || 1
                    );
                });
            }

            connectBridge(0);
        })();
        """
        return qwebchannel_js + "\n" + listener_js

    def _connect_signals(self) -> None:
        self.run_button.clicked.connect(self.open_url)
        self.url_edit.returnPressed.connect(self.open_url)
        self.back_button.clicked.connect(self.browser.back)
        self.forward_button.clicked.connect(self.browser.forward)
        self.reload_button.clicked.connect(self.browser.reload)
        self.stop_button.clicked.connect(self.browser.stop)

        self.width_spin.liveValueChanged.connect(self._on_dimension_changed)
        self.height_spin.liveValueChanged.connect(self._on_dimension_changed)
        self.overlay_checkbox.toggled.connect(self._set_overlay_visible)
        self.browser_scroll.viewportResized.connect(self._position_coordinate_overlay)

        self.bridge.pointerMoved.connect(self._on_pointer_moved)
        self.bridge.pointerLeft.connect(self._on_pointer_left)
        self.bridge.clicked.connect(self._on_page_click)
        self.bridge.viewportChanged.connect(self._on_actual_viewport_changed)

        self.browser.urlChanged.connect(self._on_url_changed)
        self.browser.loadStarted.connect(lambda: self.status_label.setText("در حال بارگذاری صفحه…"))
        self.browser.loadProgress.connect(self._on_load_progress)
        self.browser.loadFinished.connect(self._on_load_finished)

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_url)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.browser.reload)
        QShortcut(QKeySequence("F5"), self, activated=self.browser.reload)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.browser.back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self.browser.forward)

    # ----------------------------------------------------------- Navigation
    @staticmethod
    def _normalise_url(raw_url: str) -> QUrl:
        value = raw_url.strip()
        if not value:
            raise ValueError("لطفاً آدرس سایت را وارد کن.")

        # A bare host is assumed to be HTTPS. For localhost/private IPs,
        # HTTP is usually the expected development-server scheme.
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
            host = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
            hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            is_local = hostname.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
            try:
                ip = ipaddress.ip_address(hostname.strip("[]"))
                is_local = is_local or ip.is_private or ip.is_loopback or ip.is_link_local
            except ValueError:
                pass
            value = ("http://" if is_local else "https://") + value

        url = QUrl(value)
        if not url.isValid() or url.isEmpty():
            raise ValueError("آدرس واردشده معتبر نیست.")
        if url.scheme().lower() not in {"http", "https", "file"}:
            raise ValueError("فقط آدرس‌های http، https و file پشتیبانی می‌شوند.")
        if url.scheme().lower() in {"http", "https"} and not url.host():
            raise ValueError("آدرس سایت باید یک دامنه یا IP معتبر داشته باشد.")
        return url

    def open_url(self) -> None:
        try:
            url = self._normalise_url(self.url_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "آدرس نامعتبر", str(exc))
            self.url_edit.setFocus()
            return

        self.browser.setUrl(url)
        self.status_label.setText(f"درخواست باز کردن: {url.toString()}")

    def _on_url_changed(self, url: QUrl) -> None:
        if not self.url_edit.hasFocus():
            self.url_edit.setText(url.toString())

    def _on_load_progress(self, progress: int) -> None:
        self.status_label.setText(f"در حال بارگذاری صفحه… {progress}%")

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self.status_label.setText("صفحه بارگذاری شد. موس را روی محتوای سایت حرکت بده تا مختصات زنده را ببینی.")
            # Ask the page for its current viewport dimensions after navigation.
            self.browser.page().runJavaScript(
                "[window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]",
                self._on_viewport_js_result,
            )
        else:
            self.status_label.setText(
                "بارگذاری کامل نشد؛ آدرس، اتصال اینترنت، گواهی SSL یا دسترسی سایت را بررسی کن."
            )

    def _on_viewport_js_result(self, result) -> None:
        if isinstance(result, (list, tuple)) and len(result) >= 3:
            self._on_actual_viewport_changed(float(result[0]), float(result[1]), float(result[2]))

    def _focus_url(self) -> None:
        self.url_edit.setFocus()
        self.url_edit.selectAll()

    # --------------------------------------------------------- Viewport size
    def _on_dimension_changed(self, _value: int) -> None:
        self._apply_viewport_size(self.width_spin.value(), self.height_spin.value())

    def _apply_viewport_size(self, width: int, height: int, announce: bool = True) -> None:
        width = max(MIN_VIEWPORT_SIZE, min(MAX_VIEWPORT_SIZE, int(width)))
        height = max(MIN_VIEWPORT_SIZE, min(MAX_VIEWPORT_SIZE, int(height)))
        self.target_width = width
        self.target_height = height

        # Fixed size is applied to the actual QWebEngineView, not to a drawing
        # or a scaled preview. The page therefore receives this real viewport.
        self.browser.setFixedSize(width, height)
        self.target_size_label.setText(f"اندازهٔ هدف: {width} × {height}")

        if announce:
            self.status_label.setText(
                f"ابعاد مرورگر به {width} × {height} CSS px تغییر کرد؛ اندازهٔ واقعی صفحه در حال به‌روزرسانی است."
            )
        self._position_coordinate_overlay()

    def _on_actual_viewport_changed(self, width: float, height: float, dpr: float) -> None:
        actual_w = int(round(width))
        actual_h = int(round(height))
        self.actual_viewport = (actual_w, actual_h, dpr)
        matches = actual_w == self.target_width and actual_h == self.target_height
        state = "مطابق اندازهٔ درخواستی" if matches else "اختلاف با اندازهٔ درخواستی"
        self.viewport_label.setText(
            f"Viewport واقعی سایت: {actual_w} × {actual_h} CSS px   |   "
            f"درخواست: {self.target_width} × {self.target_height}   |   DPR: {dpr:.2f}   |   {state}"
        )

    # ---------------------------------------------------------- Coordinates
    @staticmethod
    def _fmt(value: float) -> str:
        return str(int(round(value)))

    def _on_pointer_moved(
        self,
        client_x: float,
        client_y: float,
        page_x: float,
        page_y: float,
        viewport_width: float,
        viewport_height: float,
        scroll_x: float,
        scroll_y: float,
        dpr: float,
    ) -> None:
        self.last_pointer = (client_x, client_y, page_x, page_y)
        measured = (int(round(viewport_width)), int(round(viewport_height)), dpr)
        if measured[:2] != self.actual_viewport[:2] or abs(measured[2] - self.actual_viewport[2]) > 0.001:
            self._on_actual_viewport_changed(viewport_width, viewport_height, dpr)
        x = self._fmt(client_x)
        y = self._fmt(client_y)
        self.coordinate_label.setText(f"مختصات موس در viewport:  X: {x}    Y: {y}")
        self.detail_label.setText(
            f"مختصات کل صفحه: X: {self._fmt(page_x)}    Y: {self._fmt(page_y)}"
            f"     |     Scroll: X: {self._fmt(scroll_x)}  Y: {self._fmt(scroll_y)}"
        )
        if self.overlay_checkbox.isChecked():
            self.coordinate_overlay.setText(f"X: {x}    Y: {y}")
            self.coordinate_overlay.adjustSize()
            self._position_coordinate_overlay()

    def _on_pointer_left(self) -> None:
        self.coordinate_label.setText("مختصات موس در viewport:  X: —    Y: —")
        if self.overlay_checkbox.isChecked():
            self.coordinate_overlay.setText("X: —    Y: —")
            self.coordinate_overlay.adjustSize()
            self._position_coordinate_overlay()

    def _on_page_click(self, client_x: float, client_y: float, page_x: float, page_y: float) -> None:
        self.last_click = (client_x, client_y, page_x, page_y)
        self.status_label.setText(
            f"کلیک ثبت شد: viewport=({self._fmt(client_x)}, {self._fmt(client_y)})"
            f"  |  page=({self._fmt(page_x)}, {self._fmt(page_y)})"
        )

    def _set_overlay_visible(self, visible: bool) -> None:
        self.coordinate_overlay.setVisible(visible)
        if visible:
            self._position_coordinate_overlay()

    def _position_coordinate_overlay(self) -> None:
        if not hasattr(self, "coordinate_overlay"):
            return
        viewport = self.browser_scroll.viewport()
        self.coordinate_overlay.adjustSize()
        x = max(6, viewport.width() - self.coordinate_overlay.width() - 10)
        self.coordinate_overlay.move(x, 10)
        self.coordinate_overlay.raise_()

    # ------------------------------------------------------------- Downloads
    def _handle_download(self, download: QWebEngineDownloadRequest) -> None:
        suggested_name = download.downloadFileName() or "download"
        destination, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "ذخیرهٔ فایل",
            str(Path.home() / suggested_name),
            "All files (*.*)",
        )
        if not destination:
            download.cancel()
            return

        target = Path(destination)
        download.setDownloadDirectory(str(target.parent))
        download.setDownloadFileName(target.name)
        download.accept()
        self.status_label.setText(f"دانلود شروع شد: {target.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chromium browser with live viewport dimensions and mouse coordinates."
    )
    parser.add_argument("--width", type=int, default=1280, help="Viewport width in CSS pixels (default: 1280)")
    parser.add_argument("--height", type=int, default=800, help="Viewport height in CSS pixels (default: 800)")
    parser.add_argument("--url", default="", help="URL to open on startup")
    parser.add_argument("--title", default="Live Browser Viewport", help="Window title")
    args = parser.parse_args()

    if not (MIN_VIEWPORT_SIZE <= args.width <= MAX_VIEWPORT_SIZE):
        parser.error(f"--width must be between {MIN_VIEWPORT_SIZE} and {MAX_VIEWPORT_SIZE}")
    if not (MIN_VIEWPORT_SIZE <= args.height <= MAX_VIEWPORT_SIZE):
        parser.error(f"--height must be between {MIN_VIEWPORT_SIZE} and {MAX_VIEWPORT_SIZE}")
    return args


def main() -> int:
    args = parse_args()

    # Required by Qt WebEngine when OpenGL contexts are shared between widgets.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Live Browser Viewport")
    app.setStyle("Fusion")

    try:
        window = LiveBrowserWindow(
            width=args.width,
            height=args.height,
            initial_url=args.url,
            title=args.title,
        )
    except Exception as exc:
        QMessageBox.critical(
            None,
            "خطا در راه‌اندازی مرورگر",
            f"مرورگر راه‌اندازی نشد. نصب PySide6 را بررسی کن.\n\nجزئیات: {exc}",
        )
        return 1

    screen = app.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        wanted_width = max(1000, min(args.width + 55, available.width() - 35))
        wanted_height = max(620, min(args.height + 205, available.height() - 45))
        window.resize(wanted_width, wanted_height)
    else:
        window.resize(1350, 900)

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
