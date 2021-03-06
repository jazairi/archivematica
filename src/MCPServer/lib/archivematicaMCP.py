#!/usr/bin/env python2

# This file is part of Archivematica.
#
# Copyright 2010-2013 Artefactual Systems Inc. <http://artefactual.com>
#
# Archivematica is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Archivematica is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.  If not, see <http://www.gnu.org/licenses/>.

# @package Archivematica
# @subpackage MCPServer
# @author Joseph Perry <joseph@artefactual.com>

# ~DOC~
#
# --- This is the MCP (master control program) ---
# The intention of this program is to provide a centralized automated distributed system for performing an arbitrary set of tasks on a directory.
# Distributed in that the work can be performed on more than one physical computer simultaneously.
# Centralized in that there is one centre point for configuring flow through the system.
# Automated in that the tasks performed will be based on the config files and instantiated for each of the targets.
#
# It loads configurations from the database.
#
# stdlib, alphabetical by import source
import logging
import logging.config
import getpass
import os
import re
import signal
import sys
import threading
import time

import django
django.setup()
from django.conf import settings as django_settings
from django.db.models import Q
from django.utils import six
from prometheus_client import start_http_server

# This project, alphabetical by import source
import watchDirectory
from utils import log_exceptions

from executor import Executor
from taskGroupRunner import TaskGroupRunner
import processing
from jobChain import jobChain
from unitSIP import unitSIP
from unitDIP import unitDIP
from unitTransfer import unitTransfer
from utils import valid_uuid
from workflow import load as load_workflow, SchemaValidationError
import RPCServer

from archivematicaFunctions import unicodeToStr
from databaseFunctions import auto_close_db, createSIP, getUTCDate
import dicts

from main.models import Job, SIP, Task

logger = logging.getLogger("archivematica.mcp.server")

# time to sleep to allow db to be updated with the new location of a SIP
dbWaitSleep = 2

stopSignalReceived = False  # Tracks whether a sigkill has been received or not

ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(os.path.join(__file__))), "assets")

DEFAULT_WORKFLOW = os.path.join(ASSETS_DIR, "workflow.json")


def fetchUUIDFromPath(path):
    # find UUID on end of SIP path
    uuidLen = -36
    if valid_uuid(path[uuidLen - 1:-1]):
        return path[uuidLen - 1:-1]


def findOrCreateSipInDB(path, waitSleep=dbWaitSleep, unit_type='SIP'):
    """Matches a directory to a database sip by it's appended UUID, or path. If it doesn't find one, it will create one"""
    path = path.replace(django_settings.SHARED_DIRECTORY, "%sharedPath%", 1)

    query = Q(currentpath=path)

    # Find UUID on end of SIP path
    UUID = fetchUUIDFromPath(path)
    sip = None
    if UUID:
        query = query | Q(uuid=UUID)

    sips = SIP.objects.filter(query)
    count = sips.count()
    if count > 1:
        # This might have happened because the UUID at the end of the directory
        # name corresponds to a different SIP in the database.
        # Try refiltering the queryset on path alone, and see if that brought us
        # down to a single SIP.
        sips = sips.filter(currentpath=path)
        count = sips.count()

        # Darn: we must have multiple SIPs with the same path in the database.
        # We have no reasonable way to recover from this condition.
        if count > 1:
            logger.error('More than one SIP for path %s and/or UUID %s, using first result', path, UUID)
    if count > 0:
        sip = sips[0]
        UUID = sip.uuid
        logger.info('Using existing SIP %s at %s', UUID, path)
    else:
        logger.info('Not using existing SIP %s at %s', UUID, path)

    if sip is None:
        # Create it
        # Note that if UUID is None here, a new UUID will be generated
        # and returned by the function; otherwise it returns the
        # value that was passed in.
        UUID = createSIP(path, UUID=UUID)
        logger.info('Creating SIP %s at %s', UUID, path)
    else:
        current_path = sip.currentpath
        if current_path != path and unit_type == 'SIP':
            # Ensure path provided matches path in DB
            sip.currentpath = path
            sip.save()

    return UUID


@log_exceptions
@auto_close_db
def createUnitAndJobChain(path, watched_dir, workflow):
    path = unicodeToStr(path)
    if os.path.isdir(path):
        path = path + "/"
    logger.debug('Starting chain for %s', path)
    if not os.path.exists(path):
        return
    unit = None
    unit_type = watched_dir["unit_type"]
    if os.path.isdir(path):
        if unit_type == "SIP":
            UUID = findOrCreateSipInDB(path)
            unit = unitSIP(path, UUID)
        elif unit_type == "DIP":
            UUID = findOrCreateSipInDB(path, unit_type='DIP')
            unit = unitDIP(path, UUID)
        elif unit_type == "Transfer":
            unit = unitTransfer(path)
    elif os.path.isfile(path):
        if unit_type == "Transfer":
            unit = unitTransfer(path)
    else:
        return
    jobChain(unit, watched_dir.chain, workflow)


def createUnitAndJobChainThreaded(path, watched_dir, workflow):
    try:
        logger.debug('Watching path %s', path)
        Executor.apply_async(
            createUnitAndJobChain, [path, watched_dir, workflow])
    except Exception:
        logger.exception('Error creating threads to watch directories')


def watchDirectories(workflow):
    """Start watching the watched directories defined in the workflow."""
    for watched_dir in workflow.get_wdirs():
        directory = os.path.join(
            django_settings.WATCH_DIRECTORY,
            watched_dir.path.lstrip(os.path.sep))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        for item in os.listdir(directory):
            if item == ".gitignore":
                continue
            # We should expect both bytes and unicode. See #932.
            if isinstance(item, six.binary_type):
                item = item.decode("utf-8")
            path = os.path.join(six.text_type(directory), item)
            createUnitAndJobChainThreaded(path, watched_dir, workflow)
        watchDirectory.archivematicaWatchDirectory(
            directory,
            variablesAdded=(watched_dir, workflow),
            callBackFunctionAdded=createUnitAndJobChainThreaded,
            alertOnFiles=not watched_dir["only_dirs"],
            interval=django_settings.WATCH_DIRECTORY_INTERVAL)


def signal_handler(signalReceived, frame):
    """Used to handle the stop/kill command signals (SIGKILL)"""
    logger.info('Recieved signal %s in frame %s', signalReceived, frame)
    global stopSignalReceived
    stopSignalReceived = True
    threads = threading.enumerate()
    for thread in threads:
        logger.warning('Not stopping %s %s', type(thread), thread)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(0)
    exit(0)


@log_exceptions
@auto_close_db
def debugMonitor():
    """Periodically prints out status of MCP, including whether the database lock is locked, thread count, etc."""
    while True:
        logger.debug('Debug monitor: datetime: %s', getUTCDate())
        logger.debug('Debug monitor: thread count: %s', threading.activeCount())
        time.sleep(3600)


@log_exceptions
@auto_close_db
def flushOutputs():
    while True:
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(5)


def cleanupOldDbEntriesOnNewRun():
    Job.objects.filter(currentstep=Job.STATUS_AWAITING_DECISION).delete()
    Job.objects.filter(currentstep=Job.STATUS_EXECUTING_COMMANDS).update(currentstep=Job.STATUS_FAILED)
    Task.objects.filter(exitcode=None).update(exitcode=-1, stderror="MCP shut down while processing.")


def created_shared_directory_structure():
    dirs = (
        "arrange",
        "completed",
        "completed/transfers",
        "currentlyProcessing",
        "DIPbackups",
        "failed",
        "rejected",
        "sharedMicroServiceTasksConfigs",
        "sharedMicroServiceTasksConfigs/createXmlEventsAssist",
        "sharedMicroServiceTasksConfigs/generateAIP",
        "sharedMicroServiceTasksConfigs/generateAIP/bagit",
        "sharedMicroServiceTasksConfigs/processingMCPConfigs",
        "sharedMicroServiceTasksConfigs/transcoder",
        "sharedMicroServiceTasksConfigs/transcoder/defaultIcons",
        "SIPbackups",
        "tmp",
        "watchedDirectories",
        "watchedDirectories/activeTransfers",
        "watchedDirectories/activeTransfers/baggitDirectory",
        "watchedDirectories/activeTransfers/baggitZippedDirectory",
        "watchedDirectories/activeTransfers/dataverseTransfer",
        "watchedDirectories/activeTransfers/Dspace",
        "watchedDirectories/activeTransfers/maildir",
        "watchedDirectories/activeTransfers/standardTransfer",
        "watchedDirectories/activeTransfers/TRIM",
        "watchedDirectories/approveNormalization",
        "watchedDirectories/approveSubmissionDocumentationIngest",
        "watchedDirectories/quarantined",
        "watchedDirectories/SIPCreation",
        "watchedDirectories/SIPCreation/completedTransfers",
        "watchedDirectories/SIPCreation/SIPsUnderConstruction",
        "watchedDirectories/storeAIP",
        "watchedDirectories/system",
        "watchedDirectories/system/autoProcessSIP",
        "watchedDirectories/system/autoRestructureForCompliance",
        "watchedDirectories/system/createAIC",
        "watchedDirectories/system/reingestAIP",
        "watchedDirectories/uploadDIP",
        "watchedDirectories/uploadedDIPs",
        "watchedDirectories/workFlowDecisions",
        "watchedDirectories/workFlowDecisions/compressionAIPDecisions",
        "watchedDirectories/workFlowDecisions/createDip",
        "watchedDirectories/workFlowDecisions/createTree",
        "watchedDirectories/workFlowDecisions/examineContentsChoice",
        "watchedDirectories/workFlowDecisions/extractPackagesChoice",
        "watchedDirectories/workFlowDecisions/metadataReminder",
        "watchedDirectories/workFlowDecisions/quarantineTransfer",
        "watchedDirectories/workFlowDecisions/selectFormatIDToolIngest",
        "watchedDirectories/workFlowDecisions/selectFormatIDToolTransfer",
        "www",
        "www/AIPsStore",
        "www/AIPsStore/transferBacklog",
        "www/AIPsStore/transferBacklog/arrange",
        "www/AIPsStore/transferBacklog/originals",
        "www/DIPsStore"
    )
    for d in dirs:
        d = os.path.join(django_settings.SHARED_DIRECTORY, d)
        if os.path.isdir(d):
            continue
        logger.info('Creating directory: %s', d)
        os.makedirs(d, mode=0o770)

    # Populate processing configurations
    for config in processing.BUILTIN_CONFIGS:
        processing.install_builtin_config(config)


naiveip_re = re.compile(r"""^(?:
(?P<addr>
    (?P<ipv4>\d{1,3}(?:\.\d{1,3}){3}) |         # IPv4 address
    (?P<ipv6>\[[a-fA-F0-9:]+\]) |               # IPv6 address
    (?P<fqdn>[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*) # FQDN
):)?(?P<port>\d+)$""", re.X)


def start_prometheus_http_server(addrport):
    if not addrport:
        return
    m = re.match(naiveip_re, addrport)
    if m is None:
        logger.error('[prometheus_http_server]'
                     ' "%s" is not a valid port number or address:port pair.',
                     addrport)
        return
    addr, _ipv4, _ipv6, _fqdn, port = m.groups()
    try:
        port = int(port)
    except ValueError:
        logger.error('[prometheus_http_server]'
                     ' "%r" is not a valid port number.', port)
        return
    if addr is None:
        addr = '127.0.0.1'
    start_http_server(*(port, addr))


def _except_hook_log_everything(exc_type, exc_value, exc_traceback):
    """
    Replacement for default exception handler that logs exceptions.
    """
    # Reference http://stackoverflow.com/a/16993115/2475775
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


if __name__ == '__main__':

    # Replace exception handler with one that logs exceptions
    sys.excepthook = _except_hook_log_everything

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info('This PID: %s', os.getpid())
    logger.info('User: %s', getpass.getuser())

    start_prometheus_http_server(django_settings.PROMETHEUS_HTTP_SERVER)

    with open(DEFAULT_WORKFLOW) as workflow_file:
        try:
            workflow = load_workflow(workflow_file)
        except SchemaValidationError as err:
            logger.error("Workflow validation error: %s", err)
            sys.exit(1)

    dicts.setup(
        shared_directory=django_settings.SHARED_DIRECTORY,
        processing_directory=django_settings.PROCESSING_DIRECTORY,
        watch_directory=django_settings.WATCH_DIRECTORY,
        rejected_directory=django_settings.REJECTED_DIRECTORY,
    )

    created_shared_directory_structure()

    t = threading.Thread(target=debugMonitor)
    t.daemon = True
    t.start()

    t = threading.Thread(target=flushOutputs)
    t.daemon = True
    t.start()

    Executor.init()
    TaskGroupRunner.init()

    cleanupOldDbEntriesOnNewRun()
    watchDirectories(workflow)

    # This is blocking the main thread with the worker loop
    RPCServer.start(workflow)
