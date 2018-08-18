"""Application entry-point"""

# Standard library
import os
import sys
import time
import json
import traceback
import threading

# Dependencies
from PyQt5 import QtCore, QtGui, QtQuick, QtTest

# Local libraries
from . import util, compat, control, settings, ipc

MODULE_DIR = os.path.dirname(__file__)
QML_IMPORT_DIR = os.path.join(MODULE_DIR, "qml")
APP_PATH = os.path.join(MODULE_DIR, "qml", "main.qml")
ICON_PATH = os.path.join(MODULE_DIR, "icon.ico")


class Window(QtQuick.QQuickView):
    """Main application window"""

    def __init__(self, parent=None):
        super(Window, self).__init__(None)
        self.parent = parent

        self.setTitle(settings.WindowTitle)
        self.setResizeMode(self.SizeRootObjectToView)

        self.resize(*settings.WindowSize)
        self.setMinimumSize(QtCore.QSize(430, 300))

    def event(self, event):
        """Allow GUI to be closed upon holding Shift"""
        if event.type() == QtCore.QEvent.Close:
            modifiers = self.parent.queryKeyboardModifiers()
            shift_pressed = QtCore.Qt.ShiftModifier & modifiers
            states = self.parent.controller.states

            if shift_pressed:
                print("Force quitted..")
                self.parent.controller.host.emit("pyblishQmlCloseForced")
                event.accept()

            elif any(state in states for state in ("ready", "finished")):
                self.parent.controller.host.emit("pyblishQmlClose")
                event.accept()

            else:
                print("Not ready, hold SHIFT to force an exit")
                event.ignore()

        return super(Window, self).event(event)


class Application(QtGui.QGuiApplication):
    """Pyblish QML wrapper around QGuiApplication

    Provides production and debug launchers along with controller
    initialisation and orchestration.

    """

    shown = QtCore.pyqtSignal(QtCore.QVariant)
    hidden = QtCore.pyqtSignal()
    quitted = QtCore.pyqtSignal()
    resized = QtCore.pyqtSignal(QtCore.QVariant, QtCore.QVariant)
    published = QtCore.pyqtSignal()
    validated = QtCore.pyqtSignal()

    def __init__(self, source, targets=[]):
        super(Application, self).__init__(sys.argv)

        self.setWindowIcon(QtGui.QIcon(ICON_PATH))

        window = Window(self)
        window.statusChanged.connect(self.on_status_changed)

        engine = window.engine()
        engine.addImportPath(QML_IMPORT_DIR)

        host = ipc.client.Proxy()
        controller = control.Controller(host, targets=targets)
        controller.finished.connect(lambda: window.alert(0))

        context = engine.rootContext()
        context.setContextProperty("app", controller)

        self.window = window
        self.engine = engine
        self.controller = controller
        self.host = host
        self.clients = dict()
        self.current_client = None

        self.shown.connect(self.show)
        self.hidden.connect(self.hide)
        self.resized.connect(self.resize)
        self.quitted.connect(self.quit)
        self.published.connect(self.publish)
        self.validated.connect(self.validate)

        window.setSource(QtCore.QUrl.fromLocalFile(source))

    def on_status_changed(self, status):
        if status == QtQuick.QQuickView.Error:
            self.quit()

    def register_client(self, port):
        self.current_client = port
        self.clients[port] = {
            "lastSeen": time.time()
        }

    def deregister_client(self, port):
        self.clients.pop(port)

    @util.SlotSentinel()
    def show(self, client_settings=None):
        """Display GUI

        Once the QML interface has been loaded, use this
        to display it.

        Arguments:
            port (int): Client asking to show GUI.
            client_settings (dict, optional): Visual settings, see settings.py

        """

        if client_settings:
            _winId = client_settings["winId"]
            if _winId is not None:
                vessel = QtGui.QWindow.fromWinId(_winId)
                self.window.setParent(vessel)
            else:
                vessel = self.window

            # Apply client-side settings
            settings.from_dict(client_settings)

            vessel.setWidth(client_settings["WindowSize"][0])
            vessel.setHeight(client_settings["WindowSize"][1])
            vessel.setTitle(client_settings["WindowTitle"])
            vessel.setFramePosition(
                QtCore.QPoint(
                    client_settings["WindowPosition"][0],
                    client_settings["WindowPosition"][1]
                )
            )

        message = list()
        message.append("Settings: ")
        for key, value in settings.to_dict().items():
            message.append("  %s = %s" % (key, value))

        print("\n".join(message))

        self.window.requestActivate()
        self.window.showNormal()

        # Give statemachine enough time to boot up
        if not any(state in self.controller.states
                   for state in ["ready", "finished"]):
            util.timer("ready")

            ready = QtTest.QSignalSpy(self.controller.ready)

            count = len(ready)
            ready.wait(1000)
            if len(ready) != count + 1:
                print("Warning: Could not enter ready state")

            util.timer_end("ready", "Awaited statemachine for %.2f ms")

        self.controller.show.emit()

        # Allow time for QML to initialise
        util.schedule(self.controller.reset, 500, channel="main")

    def hide(self):
        """Hide GUI

        Process remains active and may be shown
        via a call to `show()`

        """

        self.window.hide()

    def resize(self, width, height):
        """Resize GUI with it's vessel (container window)
        """
        # (NOTE) Could not get it resize with container, this is a
        #   alternative
        self.window.setWidth(width)
        self.window.setHeight(height)

    def publish(self):
        """Fire up the publish sequence"""
        self.controller.publish()

    def validate(self):
        """Fire up the validation sequance"""
        self.controller.validate()

    def listen(self):
        """Listen on incoming messages from host

        TODO(marcus): We can't use this, as we are already listening on stdin
            through client.py. Do use this, we will have to find a way to
            receive multiple signals from the same stdin, and channel them
            to their corresponding source.

        """

        def _listen():
            while True:
                line = self.host.channels["parent"].get()
                payload = json.loads(line)["payload"]

                # We can't call methods directly, as we are running
                # in a thread. Instead, we emit signals that do the
                # job for us.
                signal = {
                    "show": "shown",
                    "hide": "hidden",
                    "resize": "resized",
                    "quit": "quitted",
                    "publish": "published",
                    "validate": "validated"
                }.get(payload["name"])

                if not signal:
                    print("'{name}' was unavailable.".format(
                        **payload))
                else:
                    try:
                        getattr(self, signal).emit(
                            *payload.get("args", []))
                    except Exception:
                        traceback.print_exc()

        thread = threading.Thread(target=_listen)
        thread.daemon = True
        thread.start()


def main(demo=False, aschild=False, targets=[]):
    """Start the Qt-runtime and show the window

    Arguments:
        aschild (bool, optional): Run as child of parent process

    """

    if aschild:
        print("Starting pyblish-qml")
        compat.main()
        app = Application(APP_PATH, targets)
        app.listen()

        print("Done, don't forget to call `show()`")
        return app.exec_()

    else:
        print("Starting pyblish-qml server..")
        service = ipc.service.MockService() if demo else ipc.service.Service()
        server = ipc.server.Server(service, targets=targets)

        proxy = ipc.server.Proxy(server, headless=True)
        proxy.show(settings.to_dict())

        server.listen()
        server.wait()
