#!/usr/bin/env python
# -*- coding: utf-8 -*-
""" WirelessThings LaunchPad

    Author: Matt Lloyd
    Copyright 2015 Ciseco Ltd.

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

"""
import Tkinter as tk
import ttk
import sys
import os
import subprocess
import argparse
import json
import urllib2
import httplib
import shutil
import ConfigParser
import tkMessageBox
import threading
import Queue
import zipfile
from time import time, sleep
import tkFileDialog
import fileinput
from distutils import dir_util
import stat
import socket
import select
import logging
from Tabs import *
import ScrolledText

"""
    Big TODO list

    Move advance list from json into py
    
    Any TODO's from below
"""

class LaunchPad:
    _name = "WirelessThings LaunchPad"
    _autoStartText = {
                      'enable': "Enable Autostart",
                      'disable': "Disable Autostart"
                     }
    _serviceStatusText = {
                          'checking': "Checking network for a running Message Bridge",
                          'found': "Message Bridge running on network",
                          'timeout': "Message Bridge not found on network",
                          'conflict': "There's more than one Message Bridge using the NSSID "
                         }
    password = None
    _UDPListenTimeout = 1   # timeout for UDP listen
    _networkRecheckTimeout = 30
    _networkRecheckTimer = 0
    _networkUDPTimeout = 5
    _networkUDPTimer = 0
    _checking = False
    _messageBridges = {}
    _messageBridgeQueryJSON = json.dumps({"type": "MessageBridge", "network": "ALL"})



    def __init__(self):
        if hasattr(sys,'frozen'): # only when running in py2exe this exists
            self._path = sys.prefix
        else: # otherwise this is a regular python script
            self._path = os.path.dirname(os.path.realpath(__file__))

        self.debug = False # until we read config
        self.debugArg = False # or get command line
        self.configFileDefault = "LaunchPad_defaults.cfg"
        self.configFile = "LaunchPad.cfg"
        self.appFile = "AppList.json"

        self.widthMain = 550
        self.heightMain = 300
        self.heightStatusBar = 24
        self.heightTab = self.heightMain - 40
        self.proc = []
        self.disableLaunch = False
        self.updateAvailable = False

        self._running = False
        # setup initial Logging
        logging.getLogger().setLevel(logging.NOTSET)
        self.logger = logging.getLogger('LaunchPad')
        self._ch = logging.StreamHandler()
        self._ch.setLevel(logging.WARN)    # this should be WARN by default
        self._formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self._ch.setFormatter(self._formatter)
        self.logger.addHandler(self._ch)

    # MARK: - Logging
    def initLogging(self):
        """ now we have the config file loaded and the command line args setup
            setup the loggers
            """
        self.logger.info("Setting up Loggers. Console output may stop here")

        # disable logging if no options are enabled
        if (self.debugArg == False and
            self.debug == False):
            self.logger.debug("Disabling loggers")
            # disable debug output
            self.logger.setLevel(100)
            return
        # set console level
        if (self.debugArg or self.debug):
            self.logger.debug("Setting Console debug level")
            logLevel = self.config.get('Debug', 'console_level')

            numeric_level = getattr(logging, logLevel.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError('Invalid console log level: %s' % loglevel)
            self._ch.setLevel(numeric_level)
        else:
            self._ch.setLevel(100)

    def on_execute(self):
        self.checkArgs()
        self.readConfig()
        self.initLogging()
        # check if the program has just been updated
        try:
            oldVersion = self.config.getfloat('Update', 'postupdate')
        except:
            oldVersion = False

        if oldVersion and os.path.exists("../Tools/update/{}.py".format(self.currentVersion)):
            subprocess.Popen(["./{}.py".format(self.currentVersion), oldVersion], cwd="../Tools/update/")
            # reload the config cause the post update script may be change some config
            self.readConfig()
            self.config.set('Update', 'postupdate', 'False')
            self.writeConfig()
            self.restart()

        self.loadApps()
        self.loadAppsConfigFiles()

        if self.args.noupdate:
            self.checkForUpdate()
        else:
            self.updateCheckFailed = False

        self._running = True

        self.runLaunchPad()
        self.cleanUp()

    def restart(self):
        # restart after update
        args = sys.argv[:]

        self.logger.info('Re-spawning %s' % ' '.join(args))
        args.append('-u')   # no need to check for update again
        args.insert(0, sys.executable)
        if sys.platform == 'win32':
            args = ['"%s"' % arg for arg in args]

        os.execv(sys.executable, args)

    def endLaunchPad(self):
        self.logger.info("End LaunchPad")
        if hasattr(self,'master'):
            position = self.master.geometry().split("+")
            self.config.set('LaunchPad', 'window_width_offset', position[1])
            self.config.set('LaunchPad', 'window_height_offset', position[2])
            self.master.destroy()
        self._running = False

    def cleanUp(self):
        self.logger.info("Clean up and exit")
        # disconnect resources
        # kill child's??
        for c in self.proc:
            if c.poll() == None:
                c.terminate()
        self.writeConfig()
        # stop UDP Threads
        try:
            self.tUDPSendStop.set()
            self.tUDPSend.join()
        except:
            pass
        try:
            self.tUDPListenStop.set()
            self.tUDPListen.join()
        except:
            pass

    def checkArgs(self):
        self.logger.info("Parse Args")
        parser = argparse.ArgumentParser(description=self._name)
        parser.add_argument('-u', '--noupdate',
                            help='disable checking for update',
                            action='store_false')
        parser.add_argument('-d', '--debug',
                            help='Extra Debug Output, overrides LaunchPad.cfg setting',
                            action='store_true')

        self.args = parser.parse_args()

        if self.args.debug:
            self.debugArg = True
        else:
            self.debugArg = False

    def checkForUpdate(self):
        self.logger.info("Checking for update")
        # go download version file
        self.updateCheckFailed = False
        try:
            latest = self.downloadJSONNotes("latest")
            if not latest:
                self.newVersion = False
            else:
                self.newVersion = latest["Versions"][0]

        except urllib2.HTTPError as e:

            self.logger.error('Unable to get latest version info - HTTPError = ' +
                            str(e.code))
            self.newVersion = False

        except urllib2.URLError as e:
            self.logger.error('Unable to get latest version info - URLError = ' +
                            str(e.reason))
            self.newVersion = False

        except httplib.HTTPException as e:
            self.logger.error('Unable to get latest version info - HTTPException')
            self.newVersion = False

        except Exception as e:
            import traceback
            self.logger.error('Unable to get latest version info - Exception = ' +
                            traceback.format_exc())
            self.newVersion = False

        if self.newVersion:
            self.logger.debug(
                "Latest Version: {}, Current Version: {}".format(
                              self.newVersion, self.currentVersion)
                            )
            if float(self.currentVersion) < float(self.newVersion):
                self.logger.info("New Version Available")
                self.updateAvailable = True
                self.latestVersionJSON = latest
        else:
            self.updateCheckFailed = True


    def startOfferUpdateWindow(self):
        self.logger.info("Start Offer Update Window")
        position = self.master.geometry().split("+")

        self.offerUpdateWindow = tk.Toplevel()
        self.offerUpdateWindow.geometry("+{}+{}".format(
                                                    int(position[1])+self.widthMain/6,
                                                    int(position[2])+self.heightMain/6
                                                    )
                                        )

        self.offerUpdateWindow.title("Update Available")
        self.offerUpdateWindow.resizable(0, 0)

        self.updateframe = tk.Frame(self.offerUpdateWindow, name='updateFrame',
                                    relief=tk.RAISED,
                                    borderwidth=2, width=self.widthMain,
                                    height=self.heightMain/4
                                    )
        self.updateframe.pack()

        text = ScrolledText.ScrolledText(self.updateframe, width=80,
                                         height=20,
                                         font=("TKDefaultFont", "11"),
                                         wrap=tk.WORD)

        tk.Label(self.updateframe,
                 text="There is a new version of {} available.".format(self._name)
                 ).pack(pady=5)
        tk.Label(self.updateframe,
                     text="Your current version is {}\n The latest version is {}".format(
                                                             self.currentVersion, self.newVersion)
                     ).pack()
        tk.Label(self.updateframe,
                 text="Do you want to install this update?\n\n"
                      "The update process will automatically restart the local "
                      "running Message Bridge if needed.",
                 ).pack(pady=10)

        tk.Button(self.updateframe,
                  text="No",
                  command=lambda: self.offerUpdateWindow.destroy(),
                  width=30
                  ).pack(padx=30, pady=10, side=tk.BOTTOM)
        tk.Button(self.updateframe,
                  text="Yes",
                  command=lambda: self.startUpdate(),
                  width=30
                  ).pack(padx=30, pady=10, side=tk.BOTTOM)

        versions = self.latestVersionJSON["Versions"]

        for line in self.latestVersionJSON["Prefix Text"]:
            text.insert(tk.END, "{}\n".format(line))
        text.insert(tk.END, "\n")

        text.tag_config("Title", font=("TKDefaultFont", "12", "bold"))

        for version in versions:
            if float(self.currentVersion) == version:
                break
            text.insert(tk.END, "{} Version Release Notes\n".format(version), "Title")
            text.insert(tk.END, "--------------------------------------------------\n")
            notes = self.downloadJSONNotes(version)
            if not notes:
                tkMessageBox.showerror("Upload Error", "Unable to check for the new version."
                                                        "Please check your internet connection")
                self.offerUpdateWindow.destroy()
            for line in notes[str(version)]:
                text.insert(tk.END, "* {}\n".format(line))
            text.insert(tk.END, "\n")

        text.pack(side="left", fill="both", expand=True)

    def downloadJSONNotes(self, filename):
        #Download the JSON here
        try:
            url = self.config.get('Update', 'updateurl') + self.config.get('Update', 'notesfile').format(filename)

            self.logger.info("Attempting to get the release notes from: {}".format(url))
            request = urllib2.urlopen(url)
            read_data = request.read()
            return json.loads(read_data)
        except urllib2.HTTPError as e:
            self.logger.error('Unable to get file - HTTPError = ' +
                            str(e.code))
            return False
        except urllib2.URLError as e:
            self.logger.error('Unable to get file - URLError = ' +
                            str(e.reason))
            return False
        except httplib.HTTPException as e:
            self.logger.error('Unable to get file - HTTPException')
            return False
        except Exception as e:
            import traceback
            self.logger.error('Unable to get file - Exception = ' +
                            traceback.format_exc())
            return False

    def startUpdate(self):
        self.offerUpdateWindow.destroy()
        self.updateFailed = False
        # grab zip size for progress bar length
        try:
            url = self.config.get('Update', 'updateurl') + self.config.get('Update',
                                'updatefile').format(self.newVersion)
            self.logger.info("Attempting to get file size for: {}".format(url))
            u = urllib2.urlopen(url)
            meta = u.info()
            self.file_size = int(meta.getheaders("Content-Length")[0])
        except urllib2.HTTPError as e:
            self.logger.error('Unable to get download file size - HTTPError = ' +
                            str(e.code))
            self.updateFailed = "Unable to get download file size"

        except urllib2.URLError as e:
            self.logger.error('Unable to get download file size- URLError = ' +
                            str(e.reason))
            self.updateFailed = "Unable to get download file size"

        except httplib.HTTPException as e:
            self.logger.error('Unable to get download file size- HTTPException')
            self.updateFailed = "Unable to get download file size"

        except Exception as e:
            import traceback
            self.logger.error('Unable to get download file size - Exception = ' +
                            traceback.format_exc())
            self.updateFailed = "Unable to get download file size"

        if self.updateFailed:
            tkMessageBox.showerror("Update Failed", self.updateFailed)
        else:
            position = self.master.geometry().split("+")

            self.progressWindow = tk.Toplevel()
            self.progressWindow.geometry("+{}+{}".format(
                                            int(position[1]
                                                )+self.widthMain/4,
                                            int(position[2]
                                                )+self.heightMain/4
                                                         )
                                         )
            self.progressWindow.title("Downloading Zip Files")

            tk.Label(self.progressWindow, text="Downloading Zip Progress"
                     ).pack()

            self.progressBar = tk.IntVar()
            ttk.Progressbar(self.progressWindow, orient="horizontal",
                            length=200, mode="determinate",
                            maximum=self.file_size,
                            variable=self.progressBar).pack()

            self.downloadThread = threading.Thread(target=self.downloadUpdate)
            self.progressQueue = Queue.Queue()
            self.downloadThread.start()
            self.progressUpdate()


    def progressUpdate(self):
        self.logger.info("Download Progress Update")
        value = self.progressQueue.get()
        self.progressBar.set(value)
        self.progressQueue.task_done()
        if self.updateFailed:
            self.progressWindow.destroy()
            tkMessageBox.showerror("Update Failed", self.updateFailed)
        elif value < self.file_size:
            self.master.after(1, self.progressUpdate)
        else:
            self.progressWindow.destroy()
            self.doUpdate(self.config.get('Update', 'downloaddir') +
                          self.config.get('Update',
                                          'updatefile').format(self.newVersion))

    def downloadUpdate(self):
        self.logger.info("Downloading Update Zip")

        url = (self.config.get('Update', 'updateurl') +
               self.config.get('Update', 'updatefile').format(self.newVersion))

        self.logger.info(url)
        # mk dir Download
        if not os.path.exists(self.config.get('Update', 'downloaddir')):
            os.makedirs(self.config.get('Update', 'downloaddir'))

        localFile = (self.config.get('Update', 'downloaddir') +
                     self.config.get('Update', 'updatefile'
                                     ).format(self.newVersion))

        self.logger.info(localFile)

        try:
            self.logger.info("Downloading file: {}".format(url))
            u = urllib2.urlopen(url)
            f = open(localFile, 'wb')
            meta = u.info()
            file_size = int(meta.getheaders("Content-Length")[0])
            self.logger.info("Downloading: {0} Bytes: {1}".format(url,
                                                                 file_size))

            file_size_dl = 0
            block_sz = 8192
            while True:
                buffer = u.read(block_sz)
                if not buffer:
                    break

                file_size_dl += len(buffer)
                f.write(buffer)
                p = float(file_size_dl) / file_size
                status = r"{0}  [{1:.2%}]".format(file_size_dl, p)
                status = status + chr(8)*(len(status)+1)
                self.logger.info(status)
                self.progressQueue.put(file_size_dl)

            f.close()
        except urllib2.HTTPError as e:
            self.logger.error('Unable to get download file - HTTPError = ' +
                            str(e.code))
            self.updateFailed = "Unable to get download file"

        except urllib2.URLError as e:
            self.logger.error('Unable to get download file - URLError = ' +
                            str(e.reason))
            self.updateFailed = "Unable to get download file"

        except httplib.HTTPException as e:
            self.logger.error('Unable to get download file - HTTPException')
            self.updateFailed = "Unable to get download file"

        except Exception as e:
            import traceback
            self.logger.error('Unable to get download file - Exception = ' +
                            traceback.format_exc())
            self.updateFailed = "Unable to get download file"

    def manualZipUpdate(self):
        self.logger.error("Location Zip for Update")
        self.updateFailed = False
        # if Download dir already exists, make it default to manualZipUpdate
        if os.path.exists(self.config.get('Update', 'downloaddir')):
            filename = tkFileDialog.askopenfilename(title="Please select the {} Update zip".format(self._name),
                                                    initialdir=self.config.get('Update', 'downloaddir'),
                                                    filetypes = [("Zip Files",
                                                                  "*.zip")])
        else: # otherwise, use the root folder
            filename = tkFileDialog.askopenfilename(title="Please select the {} Update zip".format(self._name),
                                                    initialdir="../",
                                                    filetypes = [("Zip Files",
                                                                  "*.zip")])
        self.logger.debug("Given file name of {}".format(filename))
        # need to check we have a valid zip file name else updateFailed
        if filename == '':
            # cancelled so do nothing
            self.updateFailed = True
            self.logger.info("Update cancelled")
        elif filename.endswith('.zip'):
            # do the update
            self.doUpdate(filename)


    def doUpdate(self, file):
        self.logger.debug("Doing Update with file: {}".format(file))

        self.zfobj = zipfile.ZipFile(file)
        self.extractDir = "../"
        # self.config.get('Update', 'downloaddir') + self.newVersion + "/"
        # if not os.path.exists(self.extractDir):
        #       os.mkdir(self.extractDir)

        self.zipFileCount = len(self.zfobj.namelist())

        position = self.master.geometry().split("+")

        self.progressWindow = tk.Toplevel()
        self.progressWindow.geometry("+{}+{}".format(
                                             int(position[1])+self.widthMain/4,
                                             int(position[2])+self.heightMain/4
                                                     )
                                     )

        self.progressWindow.title("Extracting Zip Files")

        tk.Label(self.progressWindow, text="Extracting Zip Progress").pack()

        self.progressBar = tk.IntVar()
        ttk.Progressbar(self.progressWindow, orient="horizontal",
                     length=200, mode="determinate",
                     maximum=self.zipFileCount,
                     variable=self.progressBar).pack()

        self.zipThread = threading.Thread(target=self.zipExtract)
        self.progressQueue = Queue.Queue()
        self.zipThread.start()
        self.zipProgressUpdate()

    def zipProgressUpdate(self):
        self.logger.info("Zip Progress Update")

        value = self.progressQueue.get()
        self.progressBar.set(value)
        self.progressQueue.task_done()
        if self.updateFailed:
            self.progressWindow.destroy()
            tkMessageBox.showerror("Update Failed", self.updateFailed)
        elif value < self.zipFileCount:
            self.master.after(1, self.zipProgressUpdate)
        else:
            self.updateAllAutoStarts()
            self.restartAllServices()
            self.progressWindow.destroy()
            # set update flag in config
            self.config.set('Update', 'postupdate', self.currentVersion)
            self.writeConfig()
            self.restart()

    def zipExtract(self):
        count = 0
        for name in self.zfobj.namelist():
            count += 1
            self.progressQueue.put(count)
            (dirname, filename) = os.path.split(name)
            if dirname.startswith("__MACOSX") or filename == ".DS_Store":
                pass
            else:
                self.logger.debug("Decompressing " + filename + " on " + dirname)
                self.zfobj.extract(name, self.extractDir)
                if name.endswith(".py"):
                    self.logger.debug("Setting execute bits")
                    st = os.stat(self.extractDir + name)
                    os.chmod(self.extractDir + name,
                             (st.st_mode | stat.S_IXUSR | stat.S_IXGRP)
                             )
                sleep(0.1)

    def updateAllAutoStarts(self):
        self.logger.info("Updating all installed Autostart Services")
        """
            for each app in list that has auto start 1
                check status
                    if install
                        reinstall
        """
        for app in self.appList:
            if app.get('Autostart', 0):
                if self.checkAutoStart(app['id']):
                    self.autostart(app['id'], 'enable', True)

    def restartAllServices(self):
        self.logger.info("Restarting all running services")
        """
            for each app in list that has service 1
                check status
                    if running
                        restart
        """
        for app in self.appList:
            if app.get('Service', 0):
                if self.checkStatus(app['id']):
                    self.launch(app['id'], 'restart', True)

    def runLaunchPad(self):
        try :
            self.logger.info("Running LaunchPad")

            self.master = tk.Tk()
            self.master.protocol("WM_DELETE_WINDOW", self.endLaunchPad)

            # check if the offset in the config file can be applied to this screen
            # Note: due to limitation of the tk, we can't be able to find the use of
            # multiple monitors on Windows.
            configWidth = self.config.getint('LaunchPad','window_width_offset')
            configHeight = self.config.getint('LaunchPad','window_height_offset')
            monitorWidth = self.master.winfo_screenwidth()
            monitorHeight = self.master.winfo_screenheight()
            #if the offset stored is not applicable, center the screen
            if configWidth > monitorWidth or configHeight > monitorHeight:
                    width_offset = (monitorWidth - self.widthMain)/2
                    height_offset = (monitorHeight - self.heightMain+self.heightStatusBar)/2
            else:
                #uses config
                width_offset = configWidth
                height_offset = configHeight

            self.master.geometry(
                 "{}x{}+{}+{}".format(self.widthMain,
                                      self.heightMain+self.heightStatusBar,
                                      width_offset,
                                      height_offset))

            self.master.title("WirelessThings LaunchPad v{}".format(self.currentVersion))
            #self.master.resizable(0,0)

            self.tabFrame = tk.Frame(self.master, name='tabFrame')
            self.tabFrame.pack(pady=2)

            self.initTabBar()
            self.initMain()
            self.initAdvanced()
            self.initStatusBar()

            self.tBarFrame.show()

            if self.updateAvailable:
                self.master.after(500, self.startOfferUpdateWindow)

            self.master.mainloop()

        except KeyboardInterrupt:
            self.logger.info("Keyboard Interrupt - Exiting")
            self.endLaunchPad()

        self.logger.debug("Exiting")

    def initTabBar(self):
        self.logger.info("Setting up TabBar")
        # tab button frame
        self.tBarFrame = TabBar(self.tabFrame, "Main", fname='tabBar')
        self.tBarFrame.config(relief=tk.RAISED, pady=4)

        # tab buttons
        tk.Button(self.tBarFrame, text='Quit', command=self.endLaunchPad
               ).pack(side=tk.RIGHT)
        #tk.Label(self.tBarFrame, text=self.currentVersion).pack(side=tk.RIGHT)

    def initMain(self):
        self.logger.info("Setting up Main Tab")
        iframe = Tab(self.tabFrame, "Main", fname='launchPad')
        iframe.config(relief=tk.RAISED, borderwidth=2, width=self.widthMain,
                      height=self.heightTab)
        self.tBarFrame.add(iframe)


        #canvas = tk.Canvas(iframe, bd=0, width=self.widthMain-4,
        #                       height=self.heightTab-4, highlightthickness=0)
        #canvas.grid(row=0, column=0, columnspan=3, rowspan=5)

        tk.Canvas(iframe, bd=0, highlightthickness=0, width=self.widthMain-4,
                  height=28).grid(row=1, column=1, columnspan=3)
        tk.Canvas(iframe, bd=0, highlightthickness=0, width=150,
                  height=self.heightTab-4).grid(row=1, column=1, rowspan=3)

        tk.Label(iframe, text="Select an App to Launch").grid(row=1, column=1,
                                                              sticky=tk.W)

        lbframe = tk.Frame(iframe, bd=2, relief=tk.SUNKEN)

        self.scrollbar = tk.Scrollbar(lbframe)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.appSelect = tk.Listbox(lbframe, bd=0, height=10, exportselection=0,
                                    yscrollcommand=self.scrollbar.set)
        self.appSelect.bind('<<ListboxSelect>>', self.onAppSelect)
        self.appSelect.pack()

        self.scrollbar.config(command=self.appSelect.yview)

        lbframe.grid(row=2, column=1, sticky=tk.W+tk.E+tk.N+tk.S, padx=2)

        for n in range(len(self.appList)):
            self.appSelect.insert(n, "{}: {}".format(n+1,
                                                     self.appList[n]['Name']))

        self.buttonFrame = tk.Frame(iframe)
        self.buttonFrame.grid(row=3, column=1, columnspan=3, sticky=tk.W+tk.E)

        self.initLaunchFrame()
        self.initSSRFrame()

        self.appText = tk.Label(iframe, text="", width=40, height=11,
                                relief=tk.RAISED, justify=tk.LEFT, anchor=tk.NW)
        self.appText.grid(row=2, column=3, rowspan=2, sticky=tk.W+tk.E+tk.N,
                          padx=2)

        self.appSelect.selection_set(0)
        self.onAppSelect(None)

        #self.appText.insert(tk.END, )
        #self.appText.config(state=tk.DISABLED)
        #tk.Text(iframe).grid(row=0, column=1, rowspan=2)


    def initSSRFrame(self):
        # setup start|stop|restart buttons
        self.SSRFrame = tk.Frame(self.buttonFrame, name='ssrFrame')

        tk.Button(self.SSRFrame, name='start', text="Start",
                  command=lambda: self.launch(int(self.appSelect.curselection()[0]),
                                              'start'
                                              ),
                  state=tk.DISABLED
                  ).pack(side=tk.LEFT)
        tk.Button(self.SSRFrame, name='stop', text="Stop",
                  command=lambda: self.launch(int(self.appSelect.curselection()[0]),
                                              'stop'
                                              ),
                  state=tk.DISABLED
                  ).pack(side=tk.LEFT)
        tk.Button(self.SSRFrame, name='restart', text="Restart",
                  command=lambda: self.launch(int(self.appSelect.curselection()[0]),
                                              'restart'
                                              ),
                  state=tk.DISABLED
                  ).pack(side=tk.LEFT)

        self.serviceButton = tk.Button(self.SSRFrame, name='autostart',
                                       text=self._autoStartText['enable'],
                                       state=tk.DISABLED)

    def initLaunchFrame(self):
        # launch button
        self.launchFrame = tk.Frame(self.buttonFrame, name='launchFrame')
        tk.Button(self.launchFrame, name='launch', text="Launch",
                  command=lambda: self.launch(int(self.appSelect.curselection()[0]),
                                              'launch'
                                              )
                  ).pack(side=tk.LEFT)

        if self.disableLaunch:
            self.launchFrame.children['launch'].config(state=tk.DISABLED)

    def initAdvanced(self):
        self.logger.info("Setting up Advance Tab")

        aframe = Tab(self.tabFrame, "Advanced", fname='advanced')
        aframe.config(relief=tk.RAISED, borderwidth=2, width=self.widthMain,
                      height=self.heightTab)
        self.tBarFrame.add(aframe)

        tk.Canvas(aframe, bd=0, highlightthickness=0, width=self.widthMain-4,
                  height=28).grid(row=1, column=1, columnspan=3)
        tk.Canvas(aframe, bd=0, highlightthickness=0, width=150,
                  height=self.heightTab-4).grid(row=1, column=1, rowspan=3)

        tk.Label(aframe, text="Select an Advanced Task to Launch").grid(row=1,
                                                                        columnspan=3,
                                                                        column=1,
                                                                        sticky=tk.W)

        lbframe = tk.Frame(aframe, bd=2, relief=tk.SUNKEN)

        self.scrollbar = tk.Scrollbar(lbframe)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.advanceSelect = tk.Listbox(lbframe, bd=0, height=10, exportselection=0,
                                    yscrollcommand=self.scrollbar.set)
        self.advanceSelect.bind('<<ListboxSelect>>', self.onAdvanceSelect)
        self.advanceSelect.pack()

        self.scrollbar.config(command=self.advanceSelect.yview)

        lbframe.grid(row=2, column=1, sticky=tk.W+tk.E+tk.N+tk.S, padx=2)

        for n in range(len(self.advanceList)):
            self.advanceSelect.insert(n, "{}: {}".format(n+1,
                                                     self.advanceList[n]['Name']))

        self.launchAdvanceButton = tk.Button(aframe, text="Launch",
                                      command=self.launchAdvance,)
        self.launchAdvanceButton.grid(row=3, column=1, columnspan=3)

        if self.disableLaunch:
          self.launchAdvanceButton.config(state=tk.DISABLED)

        self.advanceText = tk.Label(aframe, text="", width=40, height=11,
                              relief=tk.RAISED, justify=tk.LEFT, anchor=tk.NW)
        self.advanceText.grid(row=2, column=3, rowspan=2, sticky=tk.W+tk.E+tk.N,
                        padx=2)

        self.advanceSelect.selection_set(0)
        self.onAdvanceSelect(None)

    def initStatusBar(self):
        self.logger.info("Setting up status bar and network thread")
        self._serviceStatus = tk.StringVar()
        self._serviceStatus.set(self._serviceStatusText['checking'])
        self.statusBar = tk.Label(self.master, textvariable=self._serviceStatus, bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.statusBar.pack(side=tk.BOTTOM, fill=tk.X)

        self._initUDPListenThread()
        self._initUDPSendThread()
        self.fMessageBridgeGood = threading.Event()
        self.fMessageBridgeConflict = threading.Event()

        self.master.after(100, self.checkNetwork)
        self.master.after(500, self.checkSSRButtonsState)
        self.master.after(5000, self.checkUDPThreads)

    def checkSSRButtonsState(self):
        self.onAppSelect(None)
        self.master.after(4000, self.checkSSRButtonsState)

    def checkNetwork(self):
        """
            Check for a Message Bridge running on the network

            Do we have a replay in the Queue
                update messge
                restart timmers
            has our udp time out expired
                update message
            is it time to check again
                send out UDP
                restart timmer
            checkagain in 1s

        """
        if self.fMessageBridgeConflict.is_set():
            self._serviceStatus.set(self._serviceStatusText['conflict']+self.conflictNetwork)
            self.fMessageBridgeConflict.clear()
            self.fMessageBridgeGood.clear()
            self._checking = False
            self._networkRecheckTimer = time()
        elif self.fMessageBridgeGood.is_set():
            self._serviceStatus.set(self._serviceStatusText['found'])
            self.fMessageBridgeGood.clear()
            self._checking = False
            self._networkRecheckTimer = time()
        elif self._checking and time() - self._networkUDPTimer > self._networkUDPTimeout:
            self._serviceStatus.set(self._serviceStatusText['timeout'])
            self._checking = False

        elif time() - self._networkRecheckTimer > self._networkRecheckTimeout:
            # time to check again
            self.qUDPSend.put(self._messageBridgeQueryJSON)
            self._serviceStatus.set(self._serviceStatusText['checking'])
            self._checking = True
            self._networkRecheckTimer = time()
            self._networkUDPTimer = time()


        self.master.after(1000, self.checkNetwork)

    def checkUDPThreads(self):
        if self._running:
            if not self.tUDPListen.is_alive():
                self.logger.error("UDPThread thread stopped. Try restarting it")
                self._startUDPListenThread()
                sleep(2)
                if self.tUDPListen.is_alive():
                    self.logger.critical("Unable to restart UDP Listen Thread")
                    self.die()

            if not self.tUDPSend.is_alive():
                self.logger.error("UDPSend thread stopped. Try restarting it")
                self._startUDPSendThread()
                sleep(2)
                if self.tUDPSend.is_alive():
                    self.logger.critical("Unable to restart UDP Send Thread")
                    self.die()

            self.master.after(1000, self.checkUDPThreads)

    def _UDPListenThread(self):
        """ UDP Listen Thread
        """
        self.logger.debug("tUDPListen: UDP listen thread started")

        try:
            UDPListenSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error:
            self.logger.error("tUDPListen: Failed to create socket, Exiting")

        UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sys.platform == 'darwin':
            UDPListenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

        try:
            UDPListenSocket.bind(('', int(self.config.get('UDP', 'listen_port'))))
        except socket.error:
            self.logger.error("tUDPListen: Failed to bind port, Exiting")

        UDPListenSocket.setblocking(0)

        self.logger.debug("tUDPListen: listening")
        while not self.tUDPListenStop.is_set():
            datawaiting = select.select([UDPListenSocket], [], [], self._UDPListenTimeout)
            if datawaiting[0]:
                (data, address) = UDPListenSocket.recvfrom(8192)
                self.logger.debug("tUDPListen: Received JSON: {} From: {}".format(data, address))
                try :
                    jsonin = json.loads(data)
                except ValueError:
                    self.logger.debug("tUDPListen: Invalid JSON received")
                    continue

                if jsonin['type'] == "WirelessMessage":
                    pass
                elif jsonin['type'] == "DeviceConfigurationRequest":
                    pass
                elif jsonin['type'] == "MessageBridge":
                    # we have a MessageBridge JSON do stuff with it
                    self.logger.debug("tUDPListen: JSON of type MessageBridge")
                    self._updateMessageBridgeDetailsFromJSON(jsonin, address[0])

        self.logger.debug("tUDPListen: Thread stopping")
        try:
            UDPListenSocket.close()
        except socket.error:
            self.logger.error("tUDPListen: Failed to close socket")
        return

    def _updateMessageBridgeDetailsFromJSON(self, jsonin, address):
        # update Message Bridge entry in our list
        # TODO: this needs to be a intelligent merge not just overwrite
        if jsonin['network'] in self._messageBridges.keys():
            network = jsonin['network']
            if self._messageBridges[network]['address'] != address:
                self._messageBridges[network]['state'] = "CONFLICT"
                self.conflictNetwork = network
        else:
            # new entry store the whole packet
            jsonin['address'] = address
            self._messageBridges[jsonin['network']] = jsonin

        if self._messageBridges[jsonin['network']]['state'].upper() == "CONFLICT":
            self.fMessageBridgeConflict.set()
        elif self._messageBridges[jsonin['network']]['state'].upper() == "RUNNING":
            self.fMessageBridgeGood.set()

    def _initUDPListenThread(self):
        """ Start the UDP input thread
            """
        self.logger.debug("UDP Listen Thread init")

        self.tUDPListenStop = threading.Event()

        self._startUDPListenThread()

    def _startUDPListenThread(self):
        self.tUDPListen = threading.Thread(name='tUDPListen', target=self._UDPListenThread)
        self.tUDPListen.daemon = False

        try:
            self.tUDPListen.start()
        except:
            self.logger.error("Failed to Start the UDP listen thread")

    def _initUDPSendThread(self):
        """ Start the UDP output thread
            """
        self.logger.debug("UDP Send Thread init")

        self.qUDPSend = Queue.Queue()

        self.tUDPSendStop = threading.Event()

        self._startUDPSendThread()

    def _startUDPSendThread(self):
        self.tUDPSend = threading.Thread(target=self._UDPSendThread)
        self.tUDPSend.daemon = False

        try:
            self.tUDPSend.start()
        except:
            self.logger.error("Failed to Start the UDP send thread")

    def _UDPSendThread(self):
        """ UDP Send thread
        """
        self.logger.debug("tUDPSend: Send thread started")
        # setup the UDP send socket
        try:
            UDPSendSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as msg:
            self.logger.error("tUDPSend: Failed to create socket. Error code : {} Message : {}".format(msg[0], msg[1]))
            # tUDPSend needs to stop here
            # need to send message to user saying could not open socket
            return

        UDPSendSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        UDPSendSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        sendPort = int(self.config.get('UDP', 'send_port'))

        while not self.tUDPSendStop.is_set():
            try:
                message = self.qUDPSend.get(timeout=1)     # block for up to 30 seconds
            except Queue.Empty:
                # UDP Send que was empty
                # extrem debug message
                # self.logger.error("tUDPSend: queue is empty")
                pass
            else:
                self.logger.debug("tUDPSend: Got json to send: {}".format(message))
                try:
                    UDPSendSocket.sendto(message, ('<broadcast>', sendPort))
                    self.logger.debug("tUDPSend: Put message out via UDP")
                except socket.error as msg:
                    if msg[0] == 101:
                        try:
                            self.logger.warn("tUDPSend: External network unreachable retrying on local interface only")
                            UDPSendSocket.sendto(message, ('127.0.0.255', sendPort))
                            self.logger.debug("tUDPSend: Put message out via UDP to local only")
                        except socket.error as msg:
                            self.logger.warn("tUDPSend: Failed to send via UDP local only. Error code : {} Message: {}".format(msg[0], msg[1]))
                    else:
                        self.logger.warn("tUDPSend: Failed to send via UDP. Error code : {} Message: {}".format(msg[0], msg[1]))
                else:
                    pass
                # tidy up

                self.qUDPSend.task_done()

        self.logger.debug("tUDPSend: Thread stopping")
        try:
            UDPSendSocket.close()
        except socket.error:
            self.logger.error("tUDPSend: Failed to close socket")
        return

    def onAppSelect(self, *args):
        self.logger.debug("App select update")

        # which app is selected
        app = int(self.appSelect.curselection()[0])

        self.appText.config(text=self.appList[app]['Description'])

        # update buttons in self.launchFrame based on service or not
        if self.appList[app].get('Service', 0):
            self.launchFrame.pack_forget()
            self.SSRFrame.pack()
            if self.appList[app].get('Autostart', 0):
                self.serviceButton.pack()
            else:
                self.serviceButton.pack_forget()

            self.updateSSRButtons(app)
        else:
            self.SSRFrame.pack_forget()
            self.launchFrame.pack()

    def onAdvanceSelect(self, *args):
        self.logger.debug("Advance select update")
        #self.logger.debug(args)
        app = int(self.advanceSelect.curselection()[0])
        self.advanceText.config(text=self.advanceList[app]['Description'])

    def updateSSRButtons(self, app):
        """ Update buttons based on the state of the current selection in appList
        """
        # use status to find out if the service is currently running
        running = None
        if sys.platform == 'win32':
            pass
        else:
            # check using 'status'
            if app is not None:
                running = self.checkStatus(app)

        if running:
            self.SSRFrame.children['start'].config(state=tk.DISABLED)
            self.SSRFrame.children['stop'].config(state=tk.ACTIVE)
            self.SSRFrame.children['restart'].config(state=tk.ACTIVE)
        elif not running:
            self.SSRFrame.children['start'].config(state=tk.ACTIVE)
            self.SSRFrame.children['stop'].config(state=tk.DISABLED)
            self.SSRFrame.children['restart'].config(state=tk.DISABLED)

        # if autostart find out if installed
        if self.appList[app].get('Autostart', 0):
            installed = self.checkAutoStart(app)
            if installed == True:
                self.serviceButton.config(state=tk.ACTIVE,
                                          text=self._autoStartText['disable'],
                                          command=lambda: self.autostart(app, 'disable'))
            elif installed == False:
                self.serviceButton.config(state=tk.ACTIVE,
                                          text=self._autoStartText['enable'],
                                          command=lambda: self.autostart(app, 'enable'))
            else:
                self.serviceButton.config(state=tk.DISABLED,
                                          text=self._autoStartText['enable'])

    def checkStatus(self, app):
        running = None
        appCommand = ["./{}".format(self.appList[app]['FileName'])]

        if not self.appList[app]['Args'] == "":
            appCommand.append(self.appList[app]['Args'])

        appCommand.append("status")

        self.logger.debug("Querying {}".format(appCommand))
        output = subprocess.check_output(appCommand,
                                         cwd=self.appList[app]['CWD'])
        if output.find("PID") is not -1:
            running = True
        elif output.find("not") is not -1:
            running = False
        return running

    def checkAutoStart(self, app):
        self.logger.debug("Checking autostart for app: {}".format(app))
        installed = None
        if sys.platform == 'win32':
            pass
        elif sys.platform == 'darwin':
            # OSX auto start is diffrent so pass for now
            pass
        else:
            # check init.d and rc3.d
            if os.path.exists("/etc/init.d/{}".format(self.appList[app]['InitScript'])):
                # ok script is there is it setup in rc3.d
                installed = False
                for file in os.listdir("/etc/rc3.d/"):
                    if file.find(self.appList[app]['InitScript']) is not -1:
                        installed = True
            else:
                installed = False

        return installed

    def disableSSRButtons(self):
        self.SSRFrame.children['start'].config(state=tk.DISABLED)
        self.SSRFrame.children['stop'].config(state=tk.DISABLED)
        self.SSRFrame.children['restart'].config(state=tk.DISABLED)
        self.serviceButton.config(state=tk.DISABLED)

    def autostart(self, app, command, NoUIUpdate=False):
        self.logger.info("Configer autostart for app: {}".format(app))
        if sys.platform == 'win32':
            pass
        elif sys.platform == 'darwin':
            # OSX auto start is different so pass for now
            pass
        else:
            if command == 'enable':
                self.logger.debug("Setting up init.d script for app: {}".format(app))
                self.disableSSRButtons()
                if self.password is None:
                    self.master.wait_window(PasswordDialog(self))

                if self.password == False:
                    #Cancel button was pressed
                    self.password = None
                    self.master.after(500, lambda: self.updateSSRButtons(app))
                    return
                # run update-rc.d {} remove (if there is an older script there)
                self.updateRCd(app, 'remove')
                # copy script to init.d dir
                src = (self.appList[app]['CWD'] +
                       'init.d/' +
                       self.appList[app]['InitScript']
                       )
                dst = ('/etc/init.d/' + self.appList[app]['InitScript'])

                # modify init.d script path before copying file
                for lines in fileinput.FileInput(src, inplace=1): ## edit file in place
                    if lines.startswith("cd "):
                        sys.stdout.write("cd {}/{}\n".format(self._path,
                                                             self.appList[app]['CWD']))
                    else:
                        sys.stdout.write(lines)

                # copy new modified file into init.d folder
                copyCommand = ['sudo', '-p','','-S',
                               'cp', src, dst
                               ]
                cproc = subprocess.Popen(copyCommand,
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE
                                        )
                cproc.stdin.write(self.password+'\n')
                cproc.stdin.close()
                cproc.wait()

                # need to give it execute permissions
                chCommand = ['sudo', '-p','','-S',
                             'chmod', '+x', dst
                             ]
                chproc = subprocess.Popen(chCommand,
                                          stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE
                                          )
                chproc.stdin.write(self.password+'\n')
                chproc.stdin.close()
                chproc.wait()

                # run update-rc.d {} defaults
                self.updateRCd(app, 'defaults')
                if not NoUIUpdate:
                    self.password = None
                    self.master.after(500, lambda: self.updateSSRButtons(app))

            elif command == 'disable':
                self.logger.debug("Removing init.d script for app: {}".format(app))
                self.disableSSRButtons()
                if self.password is None:
                    self.master.wait_window(PasswordDialog(self))
                # run update-rc.d {} remove
                if self.password == False:
                    #Cancel button was pressed
                    self.password = None
                    self.master.after(500, lambda: self.updateSSRButtons(app))
                    return

                self.updateRCd(app, 'remove')
                # rm script from /etc/init.d ???
                removeCommand = ['sudo', '-p','','-S',
                                 'rm',
                                 ('/etc/init.d/' +
                                  self.appList[app]['InitScript']
                                  )
                                 ]

                rproc = subprocess.Popen(removeCommand,
                                        stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE
                                        )
                rproc.stdin.write(self.password+'\n')
                rproc.stdin.close()
                rproc.wait()
                if not NoUIUpdate:
                    self.password = None
                    self.master.after(500, lambda: self.updateSSRButtons(app))


    def updateRCd(self, app, command):
        self.logger.info("Calling updateRC.d for app: {} with: {}".format(app, command))
        updateRCCommand = ['sudo','-p','','-S',
                           'update-rc.d',
                           self.appList[app]['InitScript']
                           ]
        proc = subprocess.Popen(updateRCCommand +[command],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE
                                )
        proc.stdin.write(self.password+'\n')
        proc.stdin.close()
        proc.wait()

    def launch(self, app, command, NoUIUpdate=False):
        appCommand = []
        sudo = False

        if command == "start":
            #update the cfg file for this app with the info that the app used on start
            self.reloadAppConfigFile(app)

        if command in ['stop', 'restart']:
            self._messageBridges = {}
            if os.getuid() != 0: #if program not running as su
                # check what user started the app that we want to stop/restart
                appPidFilePath = self.appsConfigFiles[app].get('Run', 'pid_file_path_name')
                appPidFileName = self.appsConfigFiles[app].get('Run', 'pid_file_name')

                if appPidFilePath.startswith("./"):
                    appPidFilePath = os.path.join(self.appList[app]['CWD'],appPidFilePath[2:])

                pidFileName = os.path.join(appPidFilePath, appPidFileName)
                with open (pidFileName, 'r') as pid_file:
                    appPID = int(pid_file.readline().rstrip())
                    pid_file.close
                    # Return UID of process pid
                    for ln in open('/proc/%d/status' % appPID):
                        if ln.startswith('Uid:'):
                            uid = int(ln.split()[1])

                if uid == 0: #if root started the app, only sudo can stop it
                    sudo = True
                    appCommand.append("sudo")
                    appCommand.append("-p")
                    appCommand.append("-S")

                    if self.password is None:
                        self.master.wait_window(PasswordDialog(self))

                    if self.password == False:
                        #Cancel button was pressed
                        self.password = None
                        self.master.after(500, lambda: self.updateSSRButtons(app))
                        return

        appCommand.append("./{}".format(self.appList[app]['FileName']))

        if not self.appList[app]['Args'] == "":
            appCommand.append(self.appList[app]['Args'])

        if self.debugArg:
            appCommand.append("-d")

        if command is not 'launch':
            appCommand.append(command)

        self.logger.debug("Verifing the apps exec permissions")
        filePath = self.appList[app]['CWD']+self.appList[app]['FileName']
        st = os.stat(filePath)
        if sys.platform == "win32": #if Windows, pass for now
            pass
        else:
            if not ((st.st_mode & stat.S_IXUSR) and (st.st_mode & stat.S_IXGRP)):
                os.chmod(filePath, (st.st_mode | stat.S_IXUSR | stat.S_IXGRP))

        self.logger.debug("Launching {}".format(appCommand))

        cproc = subprocess.Popen(appCommand,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                cwd=self.appList[app]['CWD']
                                )
        self.proc.append(cproc)

        if sudo:
            cproc.stdin.write(self.password+'\n')
            cproc.stdin.close()
            cproc.wait()

        if not NoUIUpdate and command is not 'launch':
            self.disableSSRButtons()
            if command == 'start':
                self.master.after(2000, lambda: self.updateSSRButtons(app))
            else:
                self.master.after(5000, lambda: self.updateSSRButtons(app))

    def launchAdvance(self):
        items = map(int, self.advanceSelect.curselection())
        if items:
            if items[0] == 0:
                self.manualZipUpdate()
        else:
            self.logger.debug("Nothing Selected to Launch")

    def readConfig(self):
        self.logger.info("Reading Config")

        self.config = ConfigParser.SafeConfigParser()

        # load defaults
        try:
            self.config.readfp(open(self.configFileDefault))
        except:
            self.logger.debug("Could Not Load Default Settings File")

        # read the user config file
        if not self.config.read(self.configFile):
            self.logger.debug("Could Not Load User Config, One Will be Created on Exit")

        if not self.config.sections():
            self.logger.error("No Config Loaded, Quitting")
            sys.exit()

        self.debug = self.config.getboolean('Debug', 'console_debug')

        try:
            f = open(self.config.get('Update', 'versionfile'))
            self.currentVersion = f.read().strip()
            f.close()
        except:
            pass

    def writeConfig(self):
        self.logger.debug("Writing Config")
        with open(self.configFile, 'wb') as configfile:
            self.config.write(configfile)

    def loadApps(self):
        self.logger.debug("Loading App List")
        try:
            with open(self.appFile, 'r') as f:
                read_data = f.read()
            f.closed

            self.appList = json.loads(read_data)['Apps']
            self.advanceList = json.loads(read_data)['Advanced']
        except IOError:
            self.logger.error("Could Not Load AppList File")
            self.appList = [
                            {'id': 0,
                            'Name': 'Error',
                            'FileName': None,
                            'Args': '',
                            'Description': 'Error loading AppList file'
                            }]
            self.advanceList = [
                                {'id': 0,
                                'Name': 'Error',
                                'Description': 'Error loading AppList file'
                                }]
            self.disableLaunch = True

    def loadAppsConfigFiles(self):
        self.logger.debug("Start loadAppsConfigFiles")
        self.appsConfigFiles = []
        if not self.disableLaunch:
            for app in self.appList:
                cfgFileName = "{0}{1}.cfg".format(app['CWD'],
                                            app['FileName'].split('.py',1)[0])

                cfgFileNameDefault = "{0}{1}_defaults.cfg".format(app['CWD'],
                                            app['FileName'].split('.py',1)[0])

                self.logger.info("Reading Config")

                cfgFile = ConfigParser.SafeConfigParser()

                # load defaults first
                try:
                    cfgFile.readfp(open(cfgFileNameDefault))
                except:
                    self.logger.debug("Could Not Load Default Settings File")

                # read the user config file
                if not cfgFile.read(cfgFileName):
                    self.logger.debug("Could Not Load User Config")

                if not cfgFile.sections():
                    self.logger.error("No Config Loaded, Quitting")
                    sys.exit()

                self.appsConfigFiles.append(cfgFile)

    def reloadAppConfigFile(self, app):
        self.logger.debug("Start loadAppsConfigFiles")
        cfgFileName = "{0}{1}.cfg".format(self.appList[app]['CWD'],
                                    self.appList[app]['FileName'].split('.py',1)[0])

        cfgFileNameDefault = "{0}{1}_defaults.cfg".format(self.appList[app]['CWD'],
                                    self.appList[app]['FileName'].split('.py',1)[0])

        self.logger.info("Reading Config")

        cfgFile = ConfigParser.SafeConfigParser()

        # load defaults first
        try:
            cfgFile.readfp(open(cfgFileNameDefault))
        except:
            self.logger.debug("Could Not Load Default Settings File")

        # read the user config file
        if not cfgFile.read(cfgFileName):
            self.logger.debug("Could Not Load User Config")

        if not cfgFile.sections():
            self.logger.error("No Config Loaded, Quitting")
            sys.exit()

        self.appsConfigFiles[app] = cfgFile


class PasswordDialog(tk.Toplevel):
    def __init__(self, parent):
        tk.Toplevel.__init__(self, )
        self.parent = parent
        position = self.parent.master.geometry().split("+")

        top = tk.Frame(self)
        bottom = tk.Frame(self)
        top.pack(side=tk.TOP)
        bottom.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

        self.geometry("+{}+{}".format(
                                      int(position[1]
                                          )+self.parent.widthMain/4,
                                      int(position[2]
                                          )+self.parent.heightMain/4
                                      )
                      )
        if sys.platform == 'win32':
            tk.Label(self, text="Admin permision is required to this action.\n\nPlease enter Admin password").pack(in_=top, pady=5)
        else:
            tk.Label(self, text="Root permision is required to this action.\n\nPlease enter root password").pack(in_=top, pady=5)
        self.entry = tk.Entry(self, show='*')
        self.entry.bind("<KeyRelease-Return>", self.StorePassEvent)
        self.entry.pack(in_=top, pady=5)
        submitButton = tk.Button(self)
        submitButton["text"] = "Submit"
        submitButton["command"] = self.StorePass
        submitButton.pack(in_=bottom, side=tk.LEFT, padx=35, pady=10)
        cancelButton = tk.Button(self)
        cancelButton["text"] = "Cancel"
        cancelButton["command"] = self.Cancel
        cancelButton.pack(in_=bottom, side=tk.LEFT, padx=35, pady=10)

        self.update() #make sure the window is already visible
        self.grab_set_global()


    def StorePassEvent(self, event):
        self.StorePass()

    def StorePass(self):
        self.parent.password = self.entry.get()
        self.destroy()

    def Cancel(self):
        self.parent.password = False
        self.destroy()

if __name__ == "__main__":
    app = LaunchPad()
    app.on_execute()
