import MKCommand
import PathScripts.PathLog as PathLog
import PySide.QtCore
import PySide.QtGui
import machinekit

#PathLog.setLevel(PathLog.Level.DEBUG, PathLog.thisModule())
#PathLog.trackModule(PathLog.thisModule())

class Controller(object):
    '''Class to prompt user to perform a tool change and confirm its completion.'''
    def __init__(self, mk):
        self.mk = mk
        self.mk.halUpdate.connect(self.changed)

    def isConnected(self):
        return self.mk['halrcomp'] and self.mk['halrcmd']

    def changed(self, service, msg):
        if msg.changeTool():
            if 0 == msg.toolNumber():
                PathLog.debug("TC clear")
                service.toolChanged(self.mk['halrcmd'], True)
            else:
                tc = self.getTC(msg.toolNumber())
                if tc:
                    msg = ["Insert tool #%d" % tc.ToolNumber, "<i>\"%s\"</i>" % tc.Label]
                else:
                    msg = ["Insert tool #%d" % msg.toolNumber()]
                mb = PySide.QtGui.QMessageBox()
                mb.setWindowIcon(machinekit.IconResource('machinekiticon.png'))
                mb.setWindowTitle('Machinekit')
                mb.setTextFormat(PySide.QtCore.Qt.TextFormat.RichText)
                mb.setText("<div align='center'>%s</div>" % '<br/>'.join(msg))
                mb.setIcon(PySide.QtGui.QMessageBox.Warning)
                mb.setStandardButtons(PySide.QtGui.QMessageBox.Ok | PySide.QtGui.QMessageBox.Abort)
                if PySide.QtGui.QMessageBox.Ok == mb.exec_():
                    PathLog.debug("TC confirm")
                    service.toolChanged(self.mk['halrcmd'], True)
                else:
                    PathLog.debug("TC abort")
                    self.mk['command'].sendCommand(MKCommand.MKCommandTaskAbort())
        elif msg.toolChanged():
            PathLog.debug('TC reset')
            service.toolChanged(self.mk['halrcmd'], False)
        else:
            PathLog.debug('TC -')
            pass

    def getTC(self, nr):
        job = self.mk.getJob()
        if job:
            for tc in job.ToolController:
                if tc.ToolNumber == nr:
                    return tc
        return None
