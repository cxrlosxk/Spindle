"""Spindle's native-feeling Qt desktop interface.

All Windows/Twitch I/O runs outside Qt's UI thread. The app has an intentional
manual handoff: requests in Spindle are NOT added to Apple Music Up Next.
"""
from __future__ import annotations

import asyncio
import math
import sys
import threading
import webbrowser
import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFormLayout, QFrame, QGraphicsDropShadowEffect,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget, QSizePolicy,
)
from PySide6.QtCore import QUrl

from .apple_media import AppleMusicMonitor
from .catalog import Catalog
from .commands import CommandEngine
from .queue_store import QueueStore
from .settings import Settings, data_dir
from .twitch import TwitchClient
from .native_queue import NativeQueueBridge
from .auto_queue import AutoQueueService
from .models import Track

BG = '#0d1014'
SURFACE = '#171b21'
SURFACE_RAISED = '#1c2229'
BORDER = '#303740'
TEXT = '#f3f0ea'
MUTED = '#969da6'
ACCENT = '#e0ae8d'
GREEN = '#a6ccb7'

CSS = f'''
* {{ font-family: 'Segoe UI', 'Inter', sans-serif; color: {TEXT}; font-size: 13px; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 18px; }}
QFrame#raised {{ background: {SURFACE_RAISED}; border: 1px solid #30373f; border-radius: 13px; }}
QFrame#rowCard {{ background: #1c2229; border: 1px solid #30363f; border-radius: 12px; }}
QFrame#rowCard:hover {{ border-color: #665246; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#eyebrow {{ color: {ACCENT}; font-size: 10px; font-weight: 700; letter-spacing: 2px; }}
QLabel#title {{ font-size: 31px; font-weight: 650; letter-spacing: -1px; }}
QLabel#section {{ font-size: 19px; font-weight: 600; }}
QLabel#songTitle {{ font-size: 27px; font-weight: 650; }}
QLabel#itemTitle {{ font-weight: 600; font-size: 13px; }}
QLabel#status {{ background: #24372f; color: #add4bc; border: 1px solid #395549; border-radius: 10px; padding: 5px 10px; font-size: 11px; }}
QPushButton {{ background: #262c34; border: 1px solid #404752; border-radius: 10px; padding: 9px 15px; font-weight: 600; }}
QPushButton:hover {{ background: #333a44; border-color: #696f77; }}
QPushButton:disabled {{ color: #767d85; background: #22272d; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: #181517; }}
QPushButton#primary:hover {{ background: #f0c0a1; }}
QPushButton#ghost {{ background: transparent; border: 1px solid {BORDER}; color: #d6d3cc; padding: 7px 11px; }}
QPushButton#ghost:hover {{ background: #29313a; }}
QLineEdit, QSpinBox {{ background: #11151a; border: 1px solid #3a434b; border-radius: 10px; padding: 10px 12px; selection-background-color: #765441; }}
QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: #171b21; width: 8px; border: none; border-radius: 4px; }}
QScrollBar::handle:vertical {{ background: #414852; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QDialog {{ background: {BG}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 17px; height: 17px; }}
'''


def label(text: str, kind: str | None = None, wordwrap=False) -> QLabel:
    w = QLabel(text)
    if kind:
        w.setObjectName(kind)
    if wordwrap:
        w.setWordWrap(True)
    return w


def layout_column(widget: QWidget, margin=20, spacing=12):
    box = QVBoxLayout(widget)
    box.setContentsMargins(margin, margin, margin, margin)
    box.setSpacing(spacing)
    return box


def button(text: str, kind: str = 'ghost') -> QPushButton:
    w = QPushButton(text)
    w.setObjectName(kind)
    w.setCursor(Qt.CursorShape.PointingHandCursor)
    return w


class RecordArt(QWidget):
    """Vector vinyl rather than a static logo: understated tactile identity."""
    def __init__(self):
        super().__init__()
        self.angle = 0
        self.spinning = False
        self.setMinimumSize(165, 165)
        self.setMaximumSize(235, 235)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(80)

    def tick(self):
        if self.spinning and self.isVisible():
            self.angle = (self.angle + 1.1) % 360
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 16
        x, y = (self.width()-side)/2, (self.height()-side)/2
        p.setPen(QPen(QColor('#3d424a'), 1.1))
        p.setBrush(QColor('#080a0e'))
        p.drawEllipse(int(x), int(y), int(side), int(side))
        for fraction, opacity in [(0.92, 46), (.84,30),(.74,45),(.64,32),(.56,52),(.48,35)]:
            p.setPen(QPen(QColor(180, 185, 193, opacity), 1))
            d = side * fraction
            p.drawEllipse(int(x + (side-d)/2), int(y + (side-d)/2), int(d), int(d))
        # Cream paper label, with an offset terracotta band that subtly rotates.
        center = side / 2
        d = side*.39
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#d6c4aa'))
        p.drawEllipse(int(x+center-d/2),int(y+center-d/2),int(d),int(d))
        p.save()
        p.translate(x+center, y+center)
        p.rotate(self.angle)
        p.setBrush(QColor('#a96952'))
        p.drawPie(int(-d/2), int(-d/2), int(d), int(d), 45*16, 92*16)
        p.restore()
        p.setBrush(QColor('#181a1e'))
        p.drawEllipse(int(x+center-5),int(y+center-5),10,10)
        p.end()


class Bus(QObject):
    status = Signal(str)
    track = Signal(object)
    queue_changed = Signal()
    chat = Signal(str)
    device_code = Signal(str, str)
    local_result = Signal(str)
    diagnostics_finished = Signal(str)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle('Spindle · Preferences')
        self.setMinimumWidth(450)
        outer = layout_column(self, 26, 16)
        outer.addWidget(label('Preferences', 'section'))
        desc = label('Connect a Twitch application registered as a Public Client. Spindle never needs a client secret.', 'muted', True)
        outer.addWidget(desc)
        form = QFormLayout()
        form.setSpacing(12)
        self.client = QLineEdit(settings.twitch_client_id)
        self.client.setPlaceholderText('Twitch application Client ID')
        form.addRow('Twitch Client ID', self.client)
        self.max_queue = QSpinBox(); self.max_queue.setRange(1, 200); self.max_queue.setValue(settings.max_queue)
        form.addRow('Max pending songs', self.max_queue)
        self.per_viewer = QSpinBox(); self.per_viewer.setRange(1, 15); self.per_viewer.setValue(settings.max_per_viewer)
        form.addRow('Per viewer', self.per_viewer)
        self.cooldown = QSpinBox(); self.cooldown.setRange(0, 3600); self.cooldown.setSuffix(' sec'); self.cooldown.setValue(settings.cooldown_seconds)
        form.addRow('Request cooldown', self.cooldown)
        policy_note = label(
            'The broadcaster has unlimited requests and no cooldown. '
            'Viewer and moderator limits still apply.',
            'muted', True,
        )
        outer.addWidget(policy_note)
        self.duration = QSpinBox(); self.duration.setRange(1, 120); self.duration.setSuffix(' min'); self.duration.setValue(settings.max_duration_minutes)
        form.addRow('Max song length', self.duration)
        outer.addLayout(form)
        self.enabled = QCheckBox('Allow song requests in chat')
        self.enabled.setChecked(settings.requests_enabled)
        outer.addWidget(self.enabled)
        self.native_queue_enabled = QCheckBox(
            'Automatically add Twitch requests to Apple Music Up Next (no approval)'
        )
        self.native_queue_enabled.setChecked(settings.native_queue_enabled)
        outer.addWidget(self.native_queue_enabled)
        self.protect_foreground = QCheckBox(
            'Keep games and other apps in focus (background-only queueing)'
        )
        self.protect_foreground.setChecked(settings.protect_foreground)
        outer.addWidget(self.protect_foreground)
        self.allow_focus_handoff = QCheckBox(
            'Compatibility mode: briefly focus Apple Music if background search fails'
        )
        self.allow_focus_handoff.setChecked(
            settings.allow_focus_handoff and not settings.protect_foreground
        )
        outer.addWidget(self.allow_focus_handoff)
        self.protect_foreground.toggled.connect(
            lambda enabled: self.allow_focus_handoff.setChecked(False)
            if enabled else None
        )
        self.allow_focus_handoff.toggled.connect(
            lambda enabled: self.protect_foreground.setChecked(False)
            if enabled else None
        )
        caution = label(
            'Background-only mode never deliberately activates Apple Music or '
            'sends global keystrokes. Spindle still tries native controls, '
            'but songs remain pending and retry if Apple Music requires focus. '
            'Compatibility mode may briefly interrupt your game. Automatic '
            'requests never need individual approval.',
            'muted', True,
        )
        outer.addWidget(caution)
        links = label('Register at dev.twitch.tv/console/apps. Use a Public client; Device Code authorization runs in your browser.', 'muted', True)
        outer.addWidget(links)
        actions = QHBoxLayout()
        cancel = button('Cancel')
        save = button('Save changes', 'primary')
        actions.addStretch(1); actions.addWidget(cancel); actions.addWidget(save)
        cancel.clicked.connect(self.reject); save.clicked.connect(self.accept)
        outer.addLayout(actions)

    def accept(self):
        self.settings.twitch_client_id = self.client.text().strip()
        self.settings.max_queue = self.max_queue.value()
        self.settings.max_per_viewer = self.per_viewer.value()
        self.settings.cooldown_seconds = self.cooldown.value()
        self.settings.max_duration_minutes = self.duration.value()
        self.settings.requests_enabled = self.enabled.isChecked()
        self.settings.native_queue_enabled = self.native_queue_enabled.isChecked()
        self.settings.protect_foreground = self.protect_foreground.isChecked()
        self.settings.allow_focus_handoff = (
            self.allow_focus_handoff.isChecked()
            and not self.settings.protect_foreground
        )
        self.settings.save()
        super().accept()


class SpindleWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Spindle — Apple Music for Twitch')
        self.resize(1210, 810)
        self.setMinimumSize(1010, 670)
        self.bus = Bus()
        self.settings = Settings.load()
        self.store = QueueStore(data_dir() / 'requests.db')
        self.native_queue = NativeQueueBridge()
        self.native_queue.allow_focus_handoff = self.settings.effective_focus_handoff
        self.syncing_ids: set[int] = set()
        self.sync_lock = threading.Lock()
        self.engine = CommandEngine(self.store, Catalog(), self.settings, self.bus.queue_changed.emit,
                                    native_queue=self.native_queue, on_status=self.bus.status.emit)
        # No per-request approval: Twitch commands enter the durable queue
        # immediately, and the background worker delivers in FIFO order.
        self.auto_queue = AutoQueueService(
            self.store, self.native_queue,
            lambda: self.settings.native_queue_enabled,
            on_status=self.bus.status.emit,
            on_change=self.bus.queue_changed.emit,
        )
        self.engine.deferred_native_delivery = True
        self.engine.on_request_accepted = self.auto_queue.wake
        self.monitor = AppleMusicMonitor(self.on_media, self.bus.status.emit)
        self.twitch: TwitchClient | None = None
        self.activity = []
        self.build_ui()
        self.bus.status.connect(self.on_status)
        self.bus.track.connect(self.update_track)
        self.bus.queue_changed.connect(self.refresh_queue)
        self.bus.chat.connect(self.log)
        self.bus.local_result.connect(self.local_done)
        self.bus.diagnostics_finished.connect(self.diagnostics_done)
        self.bus.device_code.connect(self.show_code)
        self.refresh_queue()
        self.start_monitor()
        self.auto_queue.start()

    def build_ui(self):
        root = QWidget(); root.setObjectName('root'); self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(34, 28, 34, 28)
        page.setSpacing(21)
        header = QHBoxLayout(); header.setSpacing(14)
        logo = label('◉'); logo.setStyleSheet(f'font-size: 34px; color: {ACCENT};')
        header.addWidget(logo)
        brand = QVBoxLayout(); brand.setSpacing(0)
        title = label('Spindle', 'title'); title.setStyleSheet('font-size: 25px; font-weight: 700;')
        brand.addWidget(title)
        sub = label('APPLE MUSIC  /  TWITCH', 'eyebrow'); brand.addWidget(sub)
        header.addLayout(brand)
        header.addStretch(1)
        self.twitch_status = label('TWITCH OFFLINE', 'status')
        self.twitch_status.setStyleSheet('background: #2b2925; color: #d8b9a2; border: 1px solid #514138; border-radius: 10px; padding: 7px 10px; font-size: 11px;')
        header.addWidget(self.twitch_status)
        self.connect_button = button('Connect Twitch', 'primary'); self.connect_button.clicked.connect(self.connect_twitch)
        header.addWidget(self.connect_button)
        self.settings_button = button('⚙  Settings'); self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        page.addLayout(header)

        top = QFrame(); top.setObjectName('card'); top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(30, 25, 32, 25); top_layout.setSpacing(24)
        self.record = RecordArt(); top_layout.addWidget(self.record)
        info = QVBoxLayout(); info.setSpacing(8)
        info.addWidget(label('NOW PLAYING  ·  APPLE MUSIC', 'eyebrow'))
        self.song_title = label('Nothing playing', 'songTitle')
        self.song_title.setWordWrap(True); info.addWidget(self.song_title)
        self.song_artist = label('Open Apple Music to get started', 'muted'); self.song_artist.setStyleSheet('font-size: 16px; color: #bab5af;')
        info.addWidget(self.song_artist)
        self.song_album = label('Waiting for Windows media session', 'muted'); info.addWidget(self.song_album)
        info.addStretch(1)
        self.now_status = label('○  Checking for Apple Music', 'muted'); info.addWidget(self.now_status)
        top_layout.addLayout(info, 1)
        top_layout.addStretch(1)
        description = QVBoxLayout(); description.setSpacing(8)
        description.addWidget(label('A LITTLE CONTEXT', 'eyebrow'))
        context = label('Spindle watches Apple Music and automatically delivers Twitch song requests. If Apple Music is unavailable, requests stay pending and are retried.', 'muted', True)
        context.setMaximumWidth(215)
        description.addWidget(context)
        description.addStretch(1)
        top_layout.addLayout(description)
        page.addWidget(top)

        content = QHBoxLayout(); content.setSpacing(20)
        left = QFrame(); left.setObjectName('card'); left.setMinimumWidth(490)
        left_col = layout_column(left, 24, 13)
        queue_title = QHBoxLayout(); queue_title.addWidget(label('Viewer requests', 'section')); queue_title.addStretch(1)
        self.count = label('0 pending', 'muted'); queue_title.addWidget(self.count)
        left_col.addLayout(queue_title)
        self.disclaimer = label('Song requests are processed automatically in order. Spindle only marks them as added after verifying Apple Music Playing Next.', 'muted', True)
        self.disclaimer.setStyleSheet('font-size: 12px; color: #b9a997;')
        left_col.addWidget(self.disclaimer)
        self.queue_scroll = QScrollArea(); self.queue_scroll.setWidgetResizable(True)
        self.queue_body = QWidget(); self.queue_list = QVBoxLayout(self.queue_body)
        self.queue_list.setContentsMargins(0, 2, 4, 0); self.queue_list.setSpacing(9)
        self.queue_scroll.setWidget(self.queue_body)
        left_col.addWidget(self.queue_scroll, 1)
        content.addWidget(left, 5)

        # The side cards have more content than a short window can display.
        # Scroll the whole sidebar instead of allowing Qt to clip card contents.
        right_container = QWidget()
        right_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        right = QVBoxLayout(right_container); right.setSpacing(16)
        right.setContentsMargins(0, 0, 10, 0)
        request_card = QFrame(); request_card.setObjectName('card'); request_card.setMinimumHeight(176)
        request_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        right_col = layout_column(request_card, 22, 12)
        right_col.addWidget(label('Find a song', 'section'))
        right_col.addWidget(label('Try a request locally, or let viewers use !sr in chat.', 'muted', True))
        input_row = QHBoxLayout(); input_row.setSpacing(8)
        self.query = QLineEdit(); self.query.setPlaceholderText('AMEN Beyoncé')
        self.query.returnPressed.connect(self.add_local)
        input_row.addWidget(self.query, 1)
        self.add_button = button('Add', 'primary'); self.add_button.clicked.connect(self.add_local)
        input_row.addWidget(self.add_button)
        right_col.addLayout(input_row)
        self.local_feedback = label('Apple catalog search · US storefront', 'muted', True)
        right_col.addWidget(self.local_feedback)
        right.addWidget(request_card)

        commands_card = QFrame(); commands_card.setObjectName('card'); commands_card.setMinimumHeight(226)
        commands_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        cmds = layout_column(commands_card, 21, 11)
        cmds.addWidget(label('Chat commands', 'section'))
        for cmd, meaning in [('!song', 'What’s playing now'), ('!sr AMEN Beyoncé', 'Request a track'),
                             ('!queue', 'Show pending songs'), ('!unrequest', 'Cancel your latest request')]:
            line = QHBoxLayout(); a=label(cmd); a.setStyleSheet(f'font-weight: 600; color: {ACCENT};')
            line.addWidget(a); line.addStretch(1); line.addWidget(label(meaning,'muted')); cmds.addLayout(line)
        more = label('Moderators: !sr on/off · !remove 2', 'muted'); cmds.addWidget(more)
        right.addWidget(commands_card)

        activity_card = QFrame(); activity_card.setObjectName('card'); activity_card.setMinimumHeight(220)
        a_col = layout_column(activity_card, 21, 10)
        activity_header = QHBoxLayout()
        activity_header.addWidget(label('Activity', 'section'))
        activity_header.addStretch(1)
        self.diagnostics_button = button('Inspect Apple Music', 'ghost')
        self.diagnostics_button.setToolTip(
            'Save a read-only, privacy-redacted report of Apple Music controls. '
            'Nothing is uploaded automatically.'
        )
        self.diagnostics_button.clicked.connect(self.save_diagnostics)
        activity_header.addWidget(self.diagnostics_button)
        a_col.addLayout(activity_header)
        self.activity_label = label('Your recent activity will appear here.', 'muted', True)
        self.activity_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        a_col.addWidget(self.activity_label, 1)
        right.addWidget(activity_card)
        right.addStretch(1)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        right_scroll.setWidget(right_container)
        right_scroll.setMinimumWidth(340)
        content.addWidget(right_scroll, 4)
        page.addLayout(content, 1)

        footer = QHBoxLayout()
        self.footer = label('Your music stays in Apple Music. Verified requests appear in Up Next.', 'muted'); footer.addWidget(self.footer)
        footer.addStretch(1)
        footer.addWidget(label('SPINDLE  /  v0.5.2 QUEUE FIX', 'eyebrow'))
        page.addLayout(footer)

    def start_monitor(self):
        threading.Thread(target=lambda: asyncio.run(self.monitor.run()), daemon=True, name='AppleMedia').start()

    def on_media(self, track):
        self.engine.set_current(track)
        self.bus.track.emit(track)

    def update_track(self, track):
        if track:
            self.song_title.setText(track.title)
            self.song_artist.setText(track.artist or 'Unknown artist')
            self.song_album.setText(track.album or 'Apple Music')
            self.now_status.setText('●  Playing' if track.playing else 'Ⅱ  Paused')
            self.record.spinning = bool(track.playing)
        else:
            self.song_title.setText('Nothing playing')
            self.song_artist.setText('Open Apple Music to get started')
            self.song_album.setText('No Apple Music media session detected')
            self.now_status.setText('○  Waiting for playback')
            self.record.spinning = False

    def on_status(self, message: str):
        self.log(message)
        lower = message.lower()
        if 'connected to twitch chat' in lower:
            self.twitch_status.setText('TWITCH CONNECTED')
            self.twitch_status.setStyleSheet('background: #24372f; color: #add4bc; border: 1px solid #395549; border-radius: 10px; padding: 7px 10px; font-size: 11px;')
            self.connect_button.setText('Connected'); self.connect_button.setEnabled(False)
        elif 'twitch disconnected' in lower or 'connection failed' in lower:
            self.twitch_status.setText('TWITCH OFFLINE')
            self.twitch_status.setStyleSheet('background: #2b2925; color: #d8b9a2; border: 1px solid #514138; border-radius: 10px; padding: 7px 10px; font-size: 11px;')
            self.connect_button.setEnabled(True); self.connect_button.setText('Reconnect Twitch')

    def log(self, message: str):
        entry = datetime.now().strftime('%H:%M') + '   ' + message
        self.activity.insert(0, entry)
        self.activity = self.activity[:7]
        self.activity_label.setText('\n\n'.join(self.activity))

    def refresh_queue(self):
        while self.queue_list.count():
            item = self.queue_list.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        pending = self.store.active()
        verified = sum(p.status == 'queued' for p in pending)
        self.count.setText(
            f'{len(pending)} requests · {verified} in Apple Music'
            + (' · auto queue on' if self.settings.native_queue_enabled else ' · auto queue off')
        )
        if not pending:
            empty = QWidget(); col=QVBoxLayout(empty); col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            disc = label('◎'); disc.setAlignment(Qt.AlignmentFlag.AlignCenter)
            disc.setStyleSheet('font-size: 50px; color: #8d7566;')
            col.addWidget(disc)
            title=label('The queue is yours'); title.setStyleSheet('font-size: 17px; font-weight: 600;')
            title.setAlignment(Qt.AlignmentFlag.AlignCenter); col.addWidget(title)
            sub=label('Requests from Twitch chat will land here.'); sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setStyleSheet(f'color: {MUTED};'); col.addWidget(sub)
            self.queue_list.addWidget(empty, 1)
        else:
            for index, request in enumerate(pending, 1):
                card = QFrame(); card.setObjectName('rowCard')
                row = QHBoxLayout(card); row.setContentsMargins(12, 10, 10, 10); row.setSpacing(11)
                num = label(f'{index:02d}')
                num.setStyleSheet(f'font-size: 14px; font-weight: 700; color: {ACCENT}; min-width: 25px;')
                row.addWidget(num)
                details=QVBoxLayout(); details.setSpacing(3)
                details.addWidget(label(request.title, 'itemTitle'))
                details.addWidget(label(f'{request.artist}  ·  @{request.viewer_name}  ·  ' +
                                        ('Added to Up Next' if request.status == 'queued' else
                                         'Waiting for automatic delivery' if self.settings.native_queue_enabled
                                         else 'Auto queue disabled'), 'muted'))
                row.addLayout(details,1)
                if request.status == 'pending':
                    # This is optional troubleshooting, not an approval step.
                    # With auto queue enabled the background worker already
                    # handles every request in arrival order.
                    sync = button(
                        'Retry' if self.settings.native_queue_enabled else 'Queue',
                        'ghost',
                    )
                    sync.setToolTip(
                        'Optional manual retry if automatic delivery is delayed.'
                        if self.settings.native_queue_enabled else
                        'Auto queue is disabled. Enable it in Settings to avoid manual steps.'
                    )
                    sync.clicked.connect(lambda _, rid=request.id: self.send_to_apple_music(rid))
                    row.addWidget(sync)
                play = button('Open', 'ghost'); play.setToolTip('Open song link. Does not alter Apple Music Up Next.')
                play.clicked.connect(lambda _, url=request.url: self.open_url(url))
                row.addWidget(play)
                done = button('✓', 'ghost'); done.setToolTip('Mark this request played manually')
                done.clicked.connect(lambda _, rid=request.id: self.resolve(rid,'played'))
                row.addWidget(done)
                remove = button('×', 'ghost'); remove.setToolTip('Only removes the Spindle record, not a song already added to Apple Music')
                remove.clicked.connect(lambda _, rid=request.id: self.resolve(rid,'removed'))
                row.addWidget(remove)
                self.queue_list.addWidget(card)
            self.queue_list.addStretch(1)

    def resolve(self, request_id, action):
        if self.store.change(request_id, action):
            self.log(f'Request #{request_id} marked {action}.')
            self.refresh_queue()

    def save_diagnostics(self):
        """Collect controls in a background COM worker; leave Apple Music alone."""
        self.diagnostics_button.setEnabled(False)
        self.log('Inspecting Apple Music controls without changing playback…')

        def work():
            try:
                report = self.native_queue.diagnose()
                # Avoid modifying the report with music metadata or credentials.
                folder = data_dir() / 'diagnostics'
                folder.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                path = folder / f'apple-music-controls-{stamp}.json'
                tmp = path.with_suffix('.tmp')
                tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
                tmp.replace(path)
                self.bus.diagnostics_finished.emit(str(path))
            except Exception as exc:
                self.bus.diagnostics_finished.emit(
                    f'ERROR: Apple Music inspection could not be saved ({type(exc).__name__}).'
                )

        threading.Thread(target=work, daemon=True, name='InspectAppleMusic').start()

    def diagnostics_done(self, result: str):
        self.diagnostics_button.setEnabled(True)
        if result.startswith('ERROR:'):
            self.log(result)
            return
        path = Path(result)
        self.log('Apple Music inspection saved to ' + str(path))
        self.log('You can upload that JSON to diagnose native queue controls. No upload was performed.')
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def send_to_apple_music(self, request_id: int):
        # Explicit retry is useful for requests made before native queue sync
        # was enabled, as well as occasional transient UIA failures.
        with self.sync_lock:
            if request_id in self.syncing_ids:
                return
            self.syncing_ids.add(request_id)
        self.log(f'Checking Apple Music Up Next for request #{request_id}…')

        def work():
            try:
                target = next((p for p in self.store.pending() if p.id == request_id), None)
                if not target:
                    self.bus.status.emit(f'Request #{request_id} is no longer pending.')
                    return
                track = Track(target.title, target.artist, target.album,
                              target.duration_ms, target.track_id, target.url)
                result = self.native_queue.enqueue(track, first_request=not bool(self.store.queued()))
                if result.verified and self.store.mark_queued(request_id):
                    self.bus.status.emit(f'Apple Music Up Next verified for {track.title} — {track.artist}')
                    self.bus.queue_changed.emit()
                else:
                    self.bus.status.emit(f'Could not verify request #{request_id} in Apple Music: {result.reason}')
            finally:
                with self.sync_lock:
                    self.syncing_ids.discard(request_id)
        threading.Thread(target=work, daemon=True, name=f'QueueRequest-{request_id}').start()

    def open_url(self, url):
        if url.startswith('https://music.apple.com/') or url.startswith('https://itunes.apple.com/'):
            QDesktopServices.openUrl(QUrl(url))

    def add_local(self):
        text = self.query.text().strip()
        if not text: return
        self.add_button.setEnabled(False)
        self.local_feedback.setText('Searching Apple’s catalog…')
        def work():
            try:
                result = self.engine.handle(
                    'local', 'Streamer', f'!sr {text}',
                    is_moderator=True, is_broadcaster=True,
                )
            except Exception as exc:
                result = f'Could not complete request: {exc}'
            self.bus.local_result.emit(result or 'Could not process that request.')
        threading.Thread(target=work, daemon=True).start()

    def local_done(self, response):
        self.add_button.setEnabled(True)
        self.local_feedback.setText(response)
        self.log(response)
        if 'pending' in response.lower(): self.query.clear()

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.native_queue.allow_focus_handoff = self.settings.effective_focus_handoff
            self.auto_queue.wake()
            self.log(
                'Automatic Apple Music requests '
                + ('enabled.' if self.settings.native_queue_enabled else 'disabled.')
            )

    def connect_twitch(self):
        if self.twitch and self.twitch.running:
            return
        if not self.settings.twitch_client_id:
            self.open_settings()
            if not self.settings.twitch_client_id:
                return
        self.connect_button.setText('Connecting…'); self.connect_button.setEnabled(False)
        self.twitch = TwitchClient(self.settings.twitch_client_id, self.engine, self.bus.status.emit,
                                   self.bus.chat.emit, self.bus.device_code.emit)
        threading.Thread(target=lambda: asyncio.run(self.twitch.run()), daemon=True, name='Twitch').start()

    def show_code(self, code, url):
        QMessageBox.information(self,'Authorize Twitch',
            f'To connect Spindle to Twitch:\n\n1. Open {url}\n2. Enter this code: {code}\n3. Approve access\n\nThe browser has been opened for you.')

    def closeEvent(self, event):
        self.monitor.stop()
        self.auto_queue.stop()
        if self.twitch: self.twitch.stop()
        # Threads are daemonic. Avoid closing SQLite while a chat command may still be active.
        super().closeEvent(event)


def launch() -> int:
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(CSS)
    window = SpindleWindow()
    window.show()
    return app.exec()
