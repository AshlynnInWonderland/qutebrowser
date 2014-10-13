# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""The main window of qutebrowser."""

import binascii
import base64

from PyQt5.QtCore import pyqtSlot, QRect, QPoint, QTimer
from PyQt5.QtWidgets import QWidget, QVBoxLayout

from qutebrowser.commands import cmdutils
from qutebrowser.config import config
from qutebrowser.utils import message, log, usertypes, qtutils, objreg, utils
from qutebrowser.widgets import tabbedbrowser, completion, downloads
from qutebrowser.widgets.statusbar import bar


class MainWindow(QWidget):

    """The main window of qutebrowser.

    Adds all needed components to a vbox, initializes subwidgets and connects
    signals.

    Attributes:
        status: The StatusBar widget.
        _downloadview: The DownloadView widget.
        _tabbed_browser: The TabbedBrowser widget.
        _vbox: The main QVBoxLayout.
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle('qutebrowser')
        state_config = objreg.get('state-config')
        try:
            data = state_config['geometry']['mainwindow']
            log.init.debug("Restoring mainwindow from {}".format(data))
            geom = base64.b64decode(data, validate=True)
        except KeyError:
            # First start
            self._set_default_geometry()
        except binascii.Error:
            log.init.exception("Error while reading geometry")
            self._set_default_geometry()
        else:
            try:
                ok = self.restoreGeometry(geom)
            except KeyError:
                log.init.exception("Error while restoring geometry.")
                self._set_default_geometry()
            if not ok:
                log.init.warning("Error while restoring geometry.")
                self._set_default_geometry()

        log.init.debug("Initial mainwindow geometry: {}".format(
            self.geometry()))
        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(0)

        self._downloadview = downloads.DownloadView()
        self._vbox.addWidget(self._downloadview)
        self._downloadview.show()

        self._tabbed_browser = tabbedbrowser.TabbedBrowser()
        self._tabbed_browser.title_changed.connect(self.setWindowTitle)
        objreg.register('tabbed-browser', self._tabbed_browser)
        self._vbox.addWidget(self._tabbed_browser)

        self._completion = completion.CompletionView(self)

        self.status = bar.StatusBar()
        self._vbox.addWidget(self.status)

        # When we're here the statusbar might not even really exist yet, so
        # resizing will fail. Therefore, we use singleShot QTimers to make sure
        # we defer this until everything else is initialized.
        QTimer.singleShot(0, self._connect_resize_completion)
        config.on_change(self.resize_completion, 'completion', 'height')
        config.on_change(self.resize_completion, 'completion', 'shrink')

        #self.retranslateUi(MainWindow)
        #self.tabWidget.setCurrentIndex(0)
        #QtCore.QMetaObject.connectSlotsByName(MainWindow)

    def __repr__(self):
        return utils.get_repr(self)

    def _connect_resize_completion(self):
        """Connect the resize_completion signal and resize it once."""
        self._completion.resize_completion.connect(self.resize_completion)
        self.resize_completion()

    def _set_default_geometry(self):
        """Set some sensible default geometry."""
        self.setGeometry(QRect(50, 50, 800, 600))

    @pyqtSlot()
    def resize_completion(self):
        """Adjust completion according to config."""
        # Get the configured height/percentage.
        confheight = str(config.get('completion', 'height'))
        if confheight.endswith('%'):
            perc = int(confheight.rstrip('%'))
            height = self.height() * perc / 100
        else:
            height = int(confheight)
        # Shrink to content size if needed and shrinking is enabled
        if config.get('completion', 'shrink'):
            contents_height = (
                self._completion.viewportSizeHint().height() +
                self._completion.horizontalScrollBar().sizeHint().height())
            if contents_height <= height:
                height = contents_height
        else:
            contents_height = -1
        # hpoint now would be the bottom-left edge of the widget if it was on
        # the top of the main window.
        topleft_y = self.height() - self.status.height() - height
        topleft_y = qtutils.check_overflow(topleft_y, 'int', fatal=False)
        topleft = QPoint(0, topleft_y)
        bottomright = self.status.geometry().topRight()
        rect = QRect(topleft, bottomright)
        if rect.isValid():
            self._completion.setGeometry(rect)

    @cmdutils.register(instance='main-window', name=['quit', 'q'])
    def close(self):
        """Quit qutebrowser.

        //

        Extend close() so we can register it as a command.
        """
        super().close()

    def resizeEvent(self, e):
        """Extend resizewindow's resizeEvent to adjust completion.

        Args:
            e: The QResizeEvent
        """
        super().resizeEvent(e)
        try:
            self.resize_completion()
            self._downloadview.updateGeometry()
            self._tabbed_browser.tabBar().refresh()
        except KeyError:
            pass

    def closeEvent(self, e):
        """Override closeEvent to display a confirmation if needed."""
        confirm_quit = config.get('ui', 'confirm-quit')
        count = self._tabbed_browser.count()
        if confirm_quit == 'never':
            e.accept()
        elif confirm_quit == 'multiple-tabs' and count <= 1:
            e.accept()
        else:
            text = "Close {} {}?".format(
                count, "tab" if count == 1 else "tabs")
            confirmed = message.ask(text, usertypes.PromptMode.yesno,
                                    default=True)
            if confirmed:
                e.accept()
            else:
                e.ignore()
