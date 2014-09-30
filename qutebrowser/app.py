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

"""Initialization of qutebrowser and application-wide things."""

import gc
import os
import sys
import subprocess
import faulthandler
import configparser
import signal
import warnings
import bdb
import base64
import functools
import traceback

from PyQt5.QtWidgets import QApplication, QDialog
from PyQt5.QtCore import (pyqtSlot, QTimer, QEventLoop, Qt, QStandardPaths,
                          qInstallMessageHandler, QObject, QUrl)

import qutebrowser
from qutebrowser.commands import userscripts, runners, cmdutils
from qutebrowser.config import style, config, websettings
from qutebrowser.network import qutescheme, proxy
from qutebrowser.browser import quickmarks, cookies, downloads, cache, hints
from qutebrowser.widgets import mainwindow, console, crash
from qutebrowser.keyinput import modeman
from qutebrowser.utils import (log, version, message, utilcmds, readline,
                               utils, qtutils, urlutils, debug, objreg,
                               usertypes)


class Application(QApplication):

    """Main application instance.

    Attributes:
        _args: ArgumentParser instance.
        _commandrunner: The main CommandRunner instance.
        _shutting_down: True if we're currently shutting down.
        _quit_status: The current quitting status.
        _crashdlg: The crash dialog currently open.
        _crashlogfile: A file handler to the fatal crash logfile.
    """

    def __init__(self, args):
        """Constructor.

        Args:
            Argument namespace from argparse.
        """
        self._quit_status = {
            'crash': True,
            'tabs': False,
            'main': False,
        }
        self._shutting_down = False
        self._crashdlg = None
        self._crashlogfile = None
        self._commandrunner = None

        if args.debug:
            # We don't enable this earlier because some imports trigger
            # warnings (which are not our fault).
            warnings.simplefilter('default')

        qt_args = qtutils.get_args(args)
        log.init.debug("Qt arguments: {}, based on {}".format(qt_args, args))
        super().__init__(qt_args)
        sys.excepthook = self._exception_hook

        self._args = args
        objreg.register('args', args)
        QTimer.singleShot(0, self._process_init_args)

        objreg.register('app', self)

        if self._args.version:
            print(version.version())
            print()
            print()
            print(qutebrowser.__copyright__)
            print()
            print(version.GPL_BOILERPLATE.strip())
            sys.exit(0)

        log.init.debug("Starting init...")
        self.setQuitOnLastWindowClosed(False)
        self.setOrganizationName("qutebrowser")
        self.setApplicationName("qutebrowser")
        self.setApplicationVersion(qutebrowser.__version__)
        utils.actute_warning()
        self._init_modules()

        log.init.debug("Initializing eventfilter...")
        mode_manager = objreg.get('mode-manager')
        self.installEventFilter(mode_manager)

        log.init.debug("Connecting signals...")
        self._connect_signals()
        modeman.enter(usertypes.KeyMode.normal, 'init')

        log.init.debug("Showing mainwindow...")
        if not args.nowindow:
            objreg.get('main-window').show()

        log.init.debug("Applying python hacks...")
        self._python_hacks()

        log.init.debug("Init done!")

        if self._crashdlg is not None:
            self._crashdlg.raise_()

    def __repr__(self):
        return utils.get_repr(self)

    def _init_modules(self):
        """Initialize all 'modules' which need to be initialized."""
        log.init.debug("Initializing message-bridge...")
        message_bridge = message.MessageBridge(self)
        objreg.register('message-bridge', message_bridge)

        log.init.debug("Initializing readline-bridge...")
        readline_bridge = readline.ReadlineBridge()
        objreg.register('readline-bridge', readline_bridge)

        log.init.debug("Initializing config...")
        config.init(self._args)
        log.init.debug("Initializing crashlog...")
        self._handle_segfault()
        log.init.debug("Initializing modes...")
        modeman.init()
        log.init.debug("Initializing websettings...")
        websettings.init()
        log.init.debug("Initializing quickmarks...")
        quickmarks.init()
        log.init.debug("Initializing proxy...")
        proxy.init()
        log.init.debug("Initializing userscripts...")
        userscripts.init()
        log.init.debug("Initializing utility commands...")
        utilcmds.init()
        log.init.debug("Initializing cookies...")
        cookie_jar = cookies.CookieJar(self)
        objreg.register('cookie-jar', cookie_jar)
        log.init.debug("Initializing cache...")
        diskcache = cache.DiskCache(self)
        objreg.register('cache', diskcache)
        log.init.debug("Initializing commands...")
        self._commandrunner = runners.CommandRunner()
        log.init.debug("Initializing search...")
        search_runner = runners.SearchRunner(self)
        objreg.register('search-runner', search_runner)
        log.init.debug("Initializing downloads...")
        download_manager = downloads.DownloadManager(self)
        objreg.register('download-manager', download_manager)
        log.init.debug("Initializing main window...")
        main_window = mainwindow.MainWindow()
        objreg.register('main-window', main_window)
        log.init.debug("Initializing debug console...")
        debug_console = console.ConsoleWidget()
        objreg.register('debug-console', debug_console)

    def _handle_segfault(self):
        """Handle a segfault from a previous run."""
        # FIXME If an empty logfile exists, we log to stdout instead, which is
        # the only way to not break multiple instances.
        # However this also means if the logfile is there for some weird
        # reason, we'll *always* log to stderr, but that's still better than no
        # dialogs at all.
        path = utils.get_standard_dir(QStandardPaths.DataLocation)
        logname = os.path.join(path, 'crash.log')
        # First check if an old logfile exists.
        if os.path.exists(logname):
            with open(logname, 'r', encoding='ascii') as f:
                data = f.read()
            if data:
                # Crashlog exists and has data in it, so something crashed
                # previously.
                try:
                    os.remove(logname)
                except PermissionError:
                    log.init.warning("Could not remove crash log!")
                else:
                    self._init_crashlogfile()
                self._crashdlg = crash.FatalCrashDialog(data)
                self._crashdlg.show()
            else:
                # Crashlog exists but without data.
                # This means another instance is probably still running and
                # didn't remove the file. As we can't write to the same file,
                # we just leave faulthandler as it is and log to stderr.
                log.init.warning("Empty crash log detected. This means either "
                                 "another instance is running (then ignore "
                                 "this warning) or the file is lying here "
                                 "because of some earlier crash (then delete "
                                 "{}).".format(logname))
                self._crashlogfile = None
        else:
            # There's no log file, so we can use this to display crashes to the
            # user on the next start.
            self._init_crashlogfile()

    def _init_crashlogfile(self):
        """Start a new logfile and redirect faulthandler to it."""
        path = utils.get_standard_dir(QStandardPaths.DataLocation)
        logname = os.path.join(path, 'crash.log')
        self._crashlogfile = open(logname, 'w', encoding='ascii')
        faulthandler.enable(self._crashlogfile)
        if (hasattr(faulthandler, 'register') and
                hasattr(signal, 'SIGUSR1')):
            # If available, we also want a traceback on SIGUSR1.
            # pylint: disable=no-member
            faulthandler.register(signal.SIGUSR1)

    def _process_init_args(self):
        """Process initial positional args.

        URLs to open have no prefix, commands to execute begin with a colon.
        """
        # QNetworkAccessManager::createRequest will hang for over a second, so
        # we make sure the GUI is refreshed here, so the start seems faster.
        self.processEvents(QEventLoop.ExcludeUserInputEvents |
                           QEventLoop.ExcludeSocketNotifiers)
        tabbed_browser = objreg.get('tabbed-browser')
        for cmd in self._args.command:
            if cmd.startswith(':'):
                log.init.debug("Startup cmd {}".format(cmd))
                self._commandrunner.run_safely_init(cmd.lstrip(':'))
            else:
                log.init.debug("Startup URL {}".format(cmd))
                try:
                    url = urlutils.fuzzy_url(cmd)
                except urlutils.FuzzyUrlError as e:
                    message.error("Error in startup argument '{}': {}".format(
                        cmd, e))
                else:
                    tabbed_browser.tabopen(url)

        if tabbed_browser.count() == 0:
            log.init.debug("Opening startpage")
            for urlstr in config.get('general', 'startpage'):
                try:
                    url = urlutils.fuzzy_url(urlstr)
                except urlutils.FuzzyUrlError as e:
                    message.error("Error when opening startpage: {}".format(e))
                else:
                    tabbed_browser.tabopen(url)

    def _python_hacks(self):
        """Get around some PyQt-oddities by evil hacks.

        This sets up the uncaught exception hook, quits with an appropriate
        exit status, and handles Ctrl+C properly by passing control to the
        Python interpreter once all 500ms.
        """
        signal.signal(signal.SIGINT, self.interrupt)
        signal.signal(signal.SIGTERM, self.interrupt)
        timer = usertypes.Timer(self, 'python_hacks')
        timer.start(500)
        timer.timeout.connect(lambda: None)
        objreg.register('python-hack-timer', timer)

    def _connect_signals(self):
        """Connect all signals to their slots."""
        # pylint: disable=too-many-statements, too-many-locals
        # syntactic sugar
        kp = objreg.get('keyparsers')
        main_window = objreg.get('main-window')
        status = main_window.status
        completion = objreg.get('completion')
        tabs = objreg.get('tabbed-browser')
        cmd = objreg.get('status-command')
        completer = objreg.get('completer')
        search_runner = objreg.get('search-runner')
        message_bridge = objreg.get('message-bridge')
        mode_manager = objreg.get('mode-manager')
        prompter = objreg.get('prompter')
        download_manager = objreg.get('download-manager')
        config_obj = objreg.get('config')
        key_config = objreg.get('key-config')

        # misc
        self.lastWindowClosed.connect(self.shutdown)
        tabs.quit.connect(self.shutdown)
        mode_manager.entered.connect(hints.on_mode_entered)

        # status bar
        mode_manager.entered.connect(status.on_mode_entered)
        mode_manager.left.connect(status.on_mode_left)
        mode_manager.left.connect(cmd.on_mode_left)
        mode_manager.left.connect(prompter.on_mode_left)

        # commands
        cmd.got_cmd.connect(self._commandrunner.run_safely)
        cmd.got_search.connect(search_runner.search)
        cmd.got_search_rev.connect(search_runner.search_rev)
        cmd.returnPressed.connect(tabs.setFocus)
        search_runner.do_search.connect(tabs.search)
        kp[usertypes.KeyMode.normal].keystring_updated.connect(
            status.keystring.setText)
        tabs.got_cmd.connect(self._commandrunner.run_safely)

        # messages
        message_bridge.s_error.connect(status.disp_error)
        message_bridge.s_info.connect(status.disp_temp_text)
        message_bridge.s_set_text.connect(status.set_text)
        message_bridge.s_maybe_reset_text.connect(status.txt.maybe_reset_text)
        message_bridge.s_set_cmd_text.connect(cmd.set_cmd_text)
        message_bridge.s_question.connect(prompter.ask_question,
                                          Qt.DirectConnection)

        # config
        config_obj.style_changed.connect(style.get_stylesheet.cache_clear)
        for obj in kp.values():
            key_config.changed.connect(obj.on_keyconfig_changed)

        # statusbar
        # FIXME some of these probably only should be triggered on mainframe
        # loadStarted.
        tabs.current_tab_changed.connect(status.prog.on_tab_changed)
        tabs.cur_progress.connect(status.prog.setValue)
        tabs.cur_load_finished.connect(status.prog.hide)
        tabs.cur_load_started.connect(status.prog.on_load_started)

        tabs.current_tab_changed.connect(status.percentage.on_tab_changed)
        tabs.cur_scroll_perc_changed.connect(status.percentage.set_perc)

        tabs.current_tab_changed.connect(status.txt.on_tab_changed)
        tabs.cur_statusbar_message.connect(status.txt.on_statusbar_message)
        tabs.cur_load_started.connect(status.txt.on_load_started)

        tabs.current_tab_changed.connect(status.url.on_tab_changed)
        tabs.cur_url_text_changed.connect(status.url.set_url)
        tabs.cur_link_hovered.connect(status.url.set_hover_url)
        tabs.cur_load_status_changed.connect(status.url.on_load_status_changed)

        # command input / completion
        mode_manager.left.connect(tabs.on_mode_left)
        cmd.clear_completion_selection.connect(
            completion.on_clear_completion_selection)
        cmd.hide_completion.connect(completion.hide)
        cmd.update_completion.connect(completer.on_update_completion)
        completer.change_completed_part.connect(cmd.on_change_completed_part)

        # downloads
        tabs.start_download.connect(download_manager.fetch)

    def _get_widgets(self):
        """Get a string list of all widgets."""
        widgets = self.allWidgets()
        widgets.sort(key=lambda e: repr(e))
        return [repr(w) for w in widgets]

    def _get_pyqt_objects(self, lines, obj, depth=0):
        """Recursive method for get_all_objects to get Qt objects."""
        for kid in obj.findChildren(QObject):
            lines.append('    ' * depth + repr(kid))
            self._get_pyqt_objects(lines, kid, depth + 1)

    def _get_registered_objects(self):
        """Get all registered objects in all registries as a string."""
        blocks = []
        lines = []
        for name, registry in objreg.meta_registry.items():
            blocks.append((name, registry.dump_objects()))
        for name, data in sorted(blocks, key=lambda e: e[0]):
            lines.append("")
            lines.append("{} object registry - {} objects:".format(
                name, len(data)))
            for line in data:
                lines.append("    {}".format(line))
        return lines

    def get_all_objects(self):
        """Get all children of an object recursively as a string."""
        output = ['']
        widget_lines = self._get_widgets()
        widget_lines = ['    ' + e for e in widget_lines]
        widget_lines.insert(0, "Qt widgets - {} objects".format(
            len(widget_lines)))
        output += widget_lines
        pyqt_lines = []
        self._get_pyqt_objects(pyqt_lines, self)
        pyqt_lines = ['    ' + e for e in pyqt_lines]
        pyqt_lines.insert(0, 'Qt objects - {} objects:'.format(
            len(pyqt_lines)))
        output += pyqt_lines
        output += ['']
        output += self._get_registered_objects()
        return '\n'.join(output)

    def _recover_pages(self):
        """Try to recover all open pages.

        Called from _exception_hook, so as forgiving as possible.

        Return:
            A list of open pages, or an empty list.
        """
        try:
            tabbed_browser = objreg.get('tabbed-browser')
        except KeyError:
            return []
        pages = []
        for tab in tabbed_browser.widgets():
            try:
                url = tab.cur_url.toString(
                    QUrl.RemovePassword | QUrl.FullyEncoded)
                if url:
                    pages.append(url)
            except Exception:  # pylint: disable=broad-except
                log.destroy.exception("Error while recovering tab")
        return pages

    def _save_geometry(self):
        """Save the window geometry to the state config."""
        state_config = objreg.get('state-config')
        data = bytes(objreg.get('main-window').saveGeometry())
        geom = base64.b64encode(data).decode('ASCII')
        try:
            state_config.add_section('geometry')
        except configparser.DuplicateSectionError:
            pass
        state_config['geometry']['mainwindow'] = geom

    def _destroy_crashlogfile(self):
        """Clean up the crash log file and delete it."""
        if self._crashlogfile is None:
            return
        # We use sys.__stderr__ instead of sys.stderr here so this will still
        # work when sys.stderr got replaced, e.g. by "Python Tools for Visual
        # Studio".
        if sys.__stderr__ is not None:
            faulthandler.enable(sys.__stderr__)
        else:
            faulthandler.disable()
        self._crashlogfile.close()
        try:
            os.remove(self._crashlogfile.name)
        except (PermissionError, FileNotFoundError):
            log.destroy.exception("Could not remove crash log!")

    def _exception_hook(self, exctype, excvalue, tb):
        """Handle uncaught python exceptions.

        It'll try very hard to write all open tabs to a file, and then exit
        gracefully.
        """
        # pylint: disable=broad-except

        if exctype is bdb.BdbQuit or not issubclass(exctype, Exception):
            # pdb exit, KeyboardInterrupt, ...
            try:
                self.shutdown()
                return
            except Exception:
                log.init.exception("Error while shutting down")
                self.quit()
                return

        exc = (exctype, excvalue, tb)
        sys.__excepthook__(*exc)

        self._quit_status['crash'] = False

        try:
            pages = self._recover_pages()
        except Exception:
            log.destroy.exception("Error while recovering pages")
            pages = []

        try:
            history = objreg.get('status-command').history[-5:]
        except Exception:
            log.destroy.exception("Error while getting history: {}")
            history = []

        try:
            objects = self.get_all_objects()
        except Exception:
            log.destroy.exception("Error while getting objects")
            objects = ""

        try:
            self.lastWindowClosed.disconnect(self.shutdown)
        except TypeError:
            log.destroy.exception("Error while preventing shutdown")
        QApplication.closeAllWindows()
        self._crashdlg = crash.ExceptionCrashDialog(pages, history, exc,
                                                    objects)
        ret = self._crashdlg.exec_()
        if ret == QDialog.Accepted:  # restore
            self.restart(shutdown=False, pages=pages)
        # We might risk a segfault here, but that's better than continuing to
        # run in some undefined state, so we only do the most needed shutdown
        # here.
        qInstallMessageHandler(None)
        self._destroy_crashlogfile()
        sys.exit(1)

    @cmdutils.register(instance='app', ignore_args=True)
    def restart(self, shutdown=True, pages=None):
        """Restart qutebrowser while keeping existing tabs open."""
        # We don't use _recover_pages here as it's too forgiving when
        # exceptions occur.
        if pages is None:
            pages = []
            for tab in objreg.get('tabbed-browser').widgets():
                urlstr = tab.cur_url.toString(
                    QUrl.RemovePassword | QUrl.FullyEncoded)
                if urlstr:
                    pages.append(urlstr)
        log.destroy.debug("sys.executable: {}".format(sys.executable))
        log.destroy.debug("sys.path: {}".format(sys.path))
        log.destroy.debug("sys.argv: {}".format(sys.argv))
        log.destroy.debug("frozen: {}".format(hasattr(sys, 'frozen')))
        if os.path.basename(sys.argv[0]) == 'qutebrowser':
            # Launched via launcher script
            args = [sys.argv[0]]
            cwd = None
        elif hasattr(sys, 'frozen'):
            args = [sys.executable]
            cwd = os.path.abspath(os.path.dirname(sys.executable))
        else:
            args = [sys.executable, '-m', 'qutebrowser']
            cwd = os.path.join(os.path.abspath(os.path.dirname(
                               qutebrowser.__file__)), '..')
        for arg in sys.argv[1:]:
            if arg.startswith('-'):
                # We only want to preserve options on a restart.
                args.append(arg)
        # Add all open pages so they get reopened.
        args += pages
        log.destroy.debug("args: {}".format(args))
        log.destroy.debug("cwd: {}".format(cwd))
        # Open a new process and immediately shutdown the existing one
        if cwd is None:
            subprocess.Popen(args)
        else:
            subprocess.Popen(args, cwd=cwd)
        if shutdown:
            self.shutdown()

    @cmdutils.register(instance='app', split=False, debug=True)
    def debug_pyeval(self, s):
        """Evaluate a python string and display the results as a webpage.

        //

        We have this here rather in utils.debug so the context of eval makes
        more sense and because we don't want to import much stuff in the utils.

        Args:
            s: The string to evaluate.
        """
        try:
            r = eval(s)  # pylint: disable=eval-used
            out = repr(r)
        except Exception:  # pylint: disable=broad-except
            out = traceback.format_exc()
        qutescheme.pyeval_output = out
        objreg.get('tabbed-browser').openurl(QUrl('qute:pyeval'), newtab=True)

    @cmdutils.register(instance='app')
    def report(self):
        """Report a bug in qutebrowser."""
        pages = self._recover_pages()
        history = objreg.get('status-command').history[-5:]
        objects = self.get_all_objects()
        self._crashdlg = crash.ReportDialog(pages, history, objects)
        self._crashdlg.show()

    def interrupt(self, signum, _frame):
        """Handler for signals to gracefully shutdown (SIGINT/SIGTERM).

        This calls self.shutdown and remaps the signal to call
        self.interrupt_forcefully the next time.
        """
        log.destroy.info("SIGINT/SIGTERM received, shutting down!")
        log.destroy.info("Do the same again to forcefully quit.")
        signal.signal(signal.SIGINT, self.interrupt_forcefully)
        signal.signal(signal.SIGTERM, self.interrupt_forcefully)
        # If we call shutdown directly here, we get a segfault.
        QTimer.singleShot(0, functools.partial(self.shutdown, 128 + signum))

    def interrupt_forcefully(self, signum, _frame):
        """Interrupt forcefully on the second SIGINT/SIGTERM request.

        This skips our shutdown routine and calls QApplication:exit instead.
        It then remaps the signals to call self.interrupt_really_forcefully the
        next time.
        """
        log.destroy.info("Forceful quit requested, goodbye cruel world!")
        log.destroy.info("Do the same again to quit with even more force.")
        signal.signal(signal.SIGINT, self.interrupt_really_forcefully)
        signal.signal(signal.SIGTERM, self.interrupt_really_forcefully)
        # This *should* work without a QTimer, but because of the trouble in
        # self.interrupt we're better safe than sorry.
        QTimer.singleShot(0, functools.partial(self.exit, 128 + signum))

    def interrupt_really_forcefully(self, signum, _frame):
        """Interrupt with even more force on the third SIGINT/SIGTERM request.

        This doesn't run *any* Qt cleanup and simply exits via Python.
        It will most likely lead to a segfault.
        """
        log.destroy.info("WHY ARE YOU DOING THIS TO ME? :(")
        sys.exit(128 + signum)

    @pyqtSlot()
    def shutdown(self, status=0):
        """Try to shutdown everything cleanly.

        For some reason lastWindowClosing sometimes seem to get emitted twice,
        so we make sure we only run once here.

        Args:
            status: The status code to exit with.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        log.destroy.debug("Shutting down with status {}...".format(status))
        prompter = objreg.get('prompter', None)
        if prompter is not None and prompter.shutdown():
            # If shutdown was called while we were asking a question, we're in
            # a still sub-eventloop (which gets quitted now) and not in the
            # main one.
            # This means we need to defer the real shutdown to when we're back
            # in the real main event loop, or we'll get a segfault.
            log.destroy.debug("Deferring real shutdown because question was "
                              "active.")
            QTimer.singleShot(0, functools.partial(self._shutdown, status))
        else:
            # If we have no questions to shut down, we are already in the real
            # event loop, so we can shut down immediately.
            self._shutdown(status)

    def _shutdown(self, status):  # noqa
        """Second stage of shutdown."""
        # pylint: disable=too-many-branches, too-many-statements
        # FIXME refactor this
        log.destroy.debug("Stage 2 of shutting down...")
        if self._args.debug_exit:
            print("Now logging shutdown.", file=sys.stderr)
            debug.trace_lines(True)
            gc.set_debug(gc.DEBUG_LEAK)
        # Remove eventfilter
        try:
            log.destroy.debug("Removing eventfilter...")
            self.removeEventFilter(objreg.get('mode-manager'))
        except KeyError:
            pass
        # Close all tabs
        try:
            log.destroy.debug("Closing tabs...")
            objreg.get('tabbed-browser').shutdown()
        except KeyError:
            pass
        # Save everything
        try:
            config_obj = objreg.get('config')
        except KeyError:
            log.destroy.debug("Config not initialized yet, so not saving "
                              "anything.")
        else:
            to_save = []
            if config.get('general', 'auto-save-config'):
                to_save.append(("config", config_obj.save))
                try:
                    key_config = objreg.get('key-config')
                except KeyError:
                    pass
                else:
                    to_save.append(("keyconfig", key_config.save))
            to_save += [("window geometry", self._save_geometry),
                        ("quickmarks", quickmarks.save)]
            try:
                command_history = objreg.get('command-history')
            except KeyError:
                pass
            else:
                to_save.append(("command history", command_history.save))
            try:
                state_config = objreg.get('state-config')
            except KeyError:
                pass
            else:
                to_save.append(("window geometry", state_config.save))
            try:
                cookie_jar = objreg.get('cookie-jar')
            except KeyError:
                pass
            else:
                to_save.append(("cookies", cookie_jar.save))
            for what, handler in to_save:
                log.destroy.debug("Saving {} (handler: {})".format(
                    what, handler.__qualname__))
                try:
                    handler()
                except AttributeError as e:
                    log.destroy.warning("Could not save {}.".format(what))
                    log.destroy.debug(e)
        # Re-enable faulthandler to stdout, then remove crash log
        log.destroy.debug("Deactiving crash log...")
        self._destroy_crashlogfile()
        # If we don't kill our custom handler here we might get segfaults
        log.destroy.debug("Deactiving message handler...")
        qInstallMessageHandler(None)
        log.destroy.debug("Cleaning up object registry...")
        objreg.clear()
        # Now we can hopefully quit without segfaults
        log.destroy.debug("Deferring QApplication::exit...")
        # We use a singleshot timer to exit here to minimize the likelyhood of
        # segfaults.
        QTimer.singleShot(0, functools.partial(self.exit, status))

    def exit(self, status):
        """Extend QApplication::exit to log the event."""
        log.destroy.debug("Now calling QApplication::exit.")
        super().exit(status)
