# The main controller classes for the Machinekit workbench and Qt integration.
#
# Client classes are expected to connect to the signals provided by the Machinekit
# class and access each service and their attributes through their hierarchical name.
#
# Look at _MKServiceRegister to find the base service names this workbench interacts
# with (with the exception of 'file' which is currently not processed by a specific
# class). 'command' and 'halrcmd' are not publish/subscribe services and therefore
# there are no attributes in their hierarchy.
#
# Similarly 'error' does not carry persistent state, it's more of a fire and forget
# type of notification. Which means once a given notification has been processed by
# at least one subscriber it is gone.
#
# The only component currently tracked in 'halrcomp' is 'fc_manualtoolchange',
# should it exist.

import FreeCAD
import MKUtils
import MachinekitInstance
import PathScripts.PathLog as PathLog
import PySide.QtCore
import PySide.QtGui
import ftplib
import io
import machinetalk.protobuf.message_pb2 as MESSAGE
import machinetalk.protobuf.types_pb2 as TYPES
import os
import threading
import time
import zmq

from MKCommand          import *
from MKServiceCommand   import *
from MKServiceError     import *
from MKServiceHal       import *
from MKServiceStatus    import *

PathLog.setLevel(PathLog.Level.INFO, PathLog.thisModule())
#PathLog.trackModule(PathLog.thisModule())

AxesForward  = ['X', 'Y', 'Z', 'A', 'B', 'C', 'U', 'V', 'W']
AxesBackward = ['x', 'y', 'z', 'a', 'b', 'c', 'u', 'v', 'w']
AxesName     = AxesForward


_MKServiceRegister = {
        'command'       : MKServiceCommand,
        'error'         : MKServiceError,
        'halrcmd'       : MKServiceHalCommand,
        'halrcomp'      : MKServiceHalStatus,
        'status'        : MKServiceStatus,
        '': None
        }

def PathSource():
    '''PathSource() ... return the path to the workbench'''
    return os.path.dirname(__file__)

def FileResource(filename):
    '''FileResource(filename) ... return the full path of the given resource file.'''
    return "%s/Resources/%s" % (PathSource(), filename)

def IconResource(filename):
    '''IconResource(filename) ... return a QtGui.QIcon from the given resource file (which must exist in the Resource directory).'''
    return PySide.QtGui.QIcon(FileResource(filename))

class Machinekit(PySide.QtCore.QObject):
    '''Class representing a MK instance and provide connection to Qt framework:
      * managing all services
      * providing access to all services 
      * deal with all the zeroconf and proto buf settings
      * trigger Qt signals on changes and upates from MK
      '''
    statusUpdate      = PySide.QtCore.Signal(object, object)
    errorUpdate       = PySide.QtCore.Signal(object, object)
    commandUpdate     = PySide.QtCore.Signal(object, object)
    halUpdate         = PySide.QtCore.Signal(object, object)
    jobUpdate         = PySide.QtCore.Signal(object)
    preferencesUpdate = PySide.QtCore.Signal()

    Context = zmq.Context()
    Poller  = zmq.Poller()

    RemoteFilename = 'FreeCAD.ngc'

    def __init__(self, instance):
        super().__init__() # for qt signals

        self.instance = instance
        self.nam = None
        self.lock = threading.Lock()

        self.service = {}
        self.socket = {}
        self.job = None
        self.needUpdateJob = True
        self.gcode = []

        for service in _MKServiceRegister:
            if service:
                self.service[service] = None
        self.lastPing = time.monotonic()

    def __str__(self):
        with self.lock:
            return "%s(%s): %s" % (self.name(), self.instance.uuid.decode(), sorted(self.instance.services()))

    def __getitem__(self, index):
        path = index.split('.')
        service = self.service.get(path[0])
        if service:
            if len(path) > 1:
                return service[path[1:]]
            return service
        return None

    def _receiveContainer(self, socket):
        msg = None
        rx = MESSAGE.Container()
        try:
            msg = socket.recv_multipart()[-1]
            rx.ParseFromString(msg)
        except Exception as e:
            PathLog.error("%s exception: %s" % (service.name, e))
            PathLog.error("    msg = '%s'" % msg)
        else:
            # ignore all ping messages for now
            if rx.type != TYPES.MT_PING:
                return rx
        return None

    def _updateServicesLocked(self):
        def removeService(s, service):
            if s and service:
                self.Poller.unregister(service.socket)
                del self.socket[service.socket]
                self.service[s] = None
                service.detach(self)
            return None

        poll = False
        # first check if all services are still available
        for s in self.service:
            ep = self.instance.endpointFor(s)
            service = self.service.get(s)
            if ep is None:
                if not service is None:
                    PathLog.debug("Removing stale service: %s.%s (%s)" % (self.name(), s, type(s)))
                    removeService(s, service)
                    if s == 'status':
                        for tn in service.topicNames():
                            self.statusUpdate.emit(service[tn], None)
            else:
                if service and ep.dsn != service.dsn:
                    PathLog.debug("Removing stale service: %s.%s" % (self.name(), s))
                    service = removeService(s, service)
                if service is None:
                    cls = _MKServiceRegister.get(s)
                    if cls is None:
                        PathLog.error("service %s not supported" % s)
                    else:
                        service = cls(self.Context, s, ep.properties)
                        PathLog.info("Connecting to %s.%-10s\t%08x" % (self.name(), s, id(service.socket)))
                        self.socket[service.socket] = service
                        self.service[s] = service
                        service.attach(self)
                        self.Poller.register(service.socket, zmq.POLLIN)
                        poll = True
                else:
                    poll = True
        return poll

    def _receiveMessage(self, socket):
        with self.lock:
            msg = None
            rx = MESSAGE.Container()
            try:
                msg = socket.recv_multipart()[-1]
                rx.ParseFromString(msg)
            except Exception as e:
                PathLog.error("%s exception: %s" % (service.name, e))
                PathLog.error("    msg = '%s'" % msg)
            else:
                # ignore all ping messages for now
                if rx.type != TYPES.MT_PING:
                    self.socket[socket].process(rx)

    def _update(self, now):
        if (now - self.lastPing) > 0.5:
            self._updateServicesLocked()
            if self.needUpdateJob:
                self.updateJob()
            for service in self.service.values():
                if service:
                    service.ping()
            self.lastPing = now

    def changed(self, service, msg):
        '''Callback invoked by the framework when one of the services received an update.'''
        if 'status.' in service.topicName():
            if ('status.task' == service.topicName() and 'file' in msg) or ('status.config' == service.topicName() and 'remote_path' in msg):
                self.updateJob()
            self.statusUpdate.emit(service, msg)
        elif 'hal' in service.topicName():
            self.halUpdate.emit(service, msg)
        elif 'error' in service.topicName():
            self.errorUpdate.emit(self, msg)
            display = PathLog.info
            if msg.isError():
                display = PathLog.error
            if msg.isText():
                display = PathLog.notice
            for m in msg.messages():
                display(m)
        elif 'command' in service.topicName():
            self.commandUpdate.emit(service, msg)

    def providesServices(self, services):
        '''Return True if the given services are detected by the receiver.'''
        if services is None:
            services = self.service
        return all([not self.service[s] is None for s in services])

    def isValid(self):
        '''Return True if the basic status and command services are available.'''
        if self['status'] is None:
            return False
        if not self['status'].isValid():
            return False
        if self['command'] is None:
            return False
        return True

    def isPowered(self):
        '''Return True if MK is powered on and ready to be used.'''
        return self.isValid() and (not self['status.io.estop']) and self['status.motion.enabled']

    def isHomed(self):
        '''Return True if all axes are homed.'''
        return self.isPowered() and all([axis.homed != 0 for axis in self['status.motion.axis']])

    def setJob(self, job):
        '''setJob(job) ... set the given job as the one currently loaded into MK.'''
        if self.job != job:
            self.job = job
            self.jobUpdate.emit(job)

    def getJob(self):
        '''Return the job that is loaded into MK.'''
        return self.job

    def name(self):
        '''Convenience function to return a good name for the receiver.'''
        if self.nam is None:
            self.nam = self['status.config.name']
            if self.nam is None:
                return self.instance.uuid.decode()
        return self.nam

    def mdi(self, cmd):
        '''mdi(cmd) ... send given g-code as MDI to MK (switch to MDI mode if necessary).'''
        command = self['command']
        if command:
            sequence = MKUtils.taskModeMDI(self)
            sequence.append(MKCommandTaskExecute(cmd))
            command.sendCommands(sequence)

    def power(self):
        '''power() ... unlocks estop and toggles power'''
        commands = []
        if self['status.io.estop']:
            commands.append(MKCommandEstop(False))
        commands.append(MKCommandPower(not self.isPowered()))

        command = self['command']
        if command:
            command.sendCommands(commands)

    def home(self):
        '''home() ... homes all axes'''
        status = self['status']
        command = self['command']

        if status and command:
            sequence = [[cmd] for cmd in MKUtils.taskModeManual(status)]
            toHome = [axis.index for axis in status['motion.axis'] if not axis.homed]
            order  = {}

            for axis in status['config.axis']:
                if axis.index in toHome:
                    batch = order.get(axis.home_sequence)
                    if batch is None:
                        batch = []
                    batch.append(axis.index)
                    order[axis.home_sequence] = batch

            for key in sorted(order):
                batch = order[key]
                sequence.append([MKCommandAxisHome(index, True) for index in batch])
                for index in batch:
                    sequence.append([MKCommandWaitUntil(lambda index=index: status["motion.axis.%d.homed" % index])])

            sequence.append(MKUtils.taskModeMDI(self, True))
            sequence.append([MKCommandTaskExecute('G10 L20 P0 X0 Y0 Z0')])

            command.sendCommandSequence(sequence)

    def boundBox(self):
        '''Return a BoundBox as defined by MK's x, y and z axes limits.'''
        x = self['status.config.axis.0.limit']
        y = self['status.config.axis.1.limit']
        z = self['status.config.axis.2.limit']
        if x is None or y is None or z is None:
            return FreeCAD.BoundBox()
        return FreeCAD.BoundBox(x.min, y.min, z.min, x.max, y.max, z.max)

    def remoteFilePath(self, path=None):
        '''remoteFilePath(path=None) ... return the path of the file on MK.'''
        if path is None:
            path = self.RemoteFilename
        base = self['status.config.remote_path']
        if base:
            return "%s/%s" % (base, path)
        return None

    def updateJob(self):
        '''Download the g-code currently loaded into MK, check if it could be a FC Path.Job.
        If the Path.Job is currently loaded trigger an update to everyone caring about these
        things.'''
        job = None
        path  = self['status.task.file']
        rpath = self.remoteFilePath()
        endpoint = self.instance.endpoint.get('file')
        PathLog.info("%s, %s, %s" % (path, rpath, endpoint))
        if path is None or rpath is None or endpoint is None:
            self.needUpdateJob = True
            self.gcode = []
        else:
            self.needUpdateJob = False
            if rpath == path:
                buf = io.BytesIO()
                ftp = ftplib.FTP()
                ftp.connect(endpoint.address(), endpoint.port())
                ftp.login()
                ftp.retrbinary("RETR %s" % self.RemoteFilename, buf.write)
                ftp.quit()

                buf.seek(0)
                self.gcode = [line.decode().strip() for line in buf]

                if len(self.gcode) > 2 and self.gcode[0].startswith('(FreeCAD.Job: ') and self.gcode[1].startswith('(FreeCAD.File: ') and self.gcode[2].startswith('(FreeCAD.Signature: '):
                    title     = self.gcode[0][14:-1]
                    filename  = self.gcode[1][15:-1]
                    signature = self.gcode[2][20:-1]
                    PathLog.debug("Loaded document: '%s' - '%s'" % (filename, title))
                    for docName, doc in FreeCAD.listDocuments().items():
                        PathLog.debug("Document: '%s' - '%s'" % (docName, doc.FileName))
                        if doc.FileName == filename:
                            job = doc.getObject(title)
                            if job:
                                sign = MKUtils.pathSignature(job.Path)
                                if str(sign) == signature:
                                    PathLog.info("Job %s.%s loaded." % (job.Document.Label, job.Label))
                                else:
                                    PathLog.warning("Job %s.%s is out of date!" % (job.Document.Label, job.Label))

        self.setJob(job)

_MachinekitInstanceMonitor = MachinekitInstance.ServiceMonitor()
_Machinekit = {}

def _update():
    '''Internal callback periodically invoked for houskeeping tasks.'''
    # first make sure we know about all MK instances
    for inst in _MachinekitInstanceMonitor.instances(None):
        if _Machinekit.get(inst.uuid) is None:
            _Machinekit[inst.uuid] = Machinekit(inst)

    # then let each MK instance update itself
    now = time.monotonic()
    for mk in _Machinekit.values():
        mk._update(now)

    # now check for any messages that need to be processed
    poll = True
    while poll:
        # As it turns out the poller most often returns a single pair so we want to call it
        # in a loop to get all pending updates.
        poll = dict(Machinekit.Poller.poll(0))
        for socket in poll:
            processed = False
            for mk in _Machinekit.values():
                if socket in mk.socket:
                    mk._receiveMessage(socket)
                    processed = True
            if not processed:
                PathLog.debug("Unconnected socket? %08x" % id(socket))

def Instances(services=None):
    '''Instances(services=None) ... Answer a list of all discovered Machinekit instances which provide all services listed.
    If no services are requested all discovered MK instances are returned.'''
    return [mk for mk in _Machinekit.values() if mk.providesServices(services)]

def Any():
    '''Any() ... returns a Machinekit instance, if at least one was discovered.'''
    for mk in _Machinekit.values():
        return mk
    return None

# these are for debugging and development - do not use
hud     = None
jog     = None
execute = None

