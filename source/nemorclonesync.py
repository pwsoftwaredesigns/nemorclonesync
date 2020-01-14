#-----[ Includes ]-------------------------------------------------
from gi.repository import Nemo, GObject, Gtk, GLib
import json
import urllib
import socket
import re
import os
import subprocess
import copy
import sys
from abc import ABCMeta, abstractmethod

#-----[ Constants ]------------------------------------------------
PLUGIN_NAME = "NemoRcloneSyncProvider"
PLUGIN_TITLE = "Nemo Rclone Sync"
VERSION = 3
DEFAULT_META_OBJECT = {
    "version": VERSION,
    "first_sync": True,
    "places": []
}
META_DIR = ".rclonesync"
META_FILE_PREFIX = "meta"
SYNC_LOG_FILE = "sync.log"
RCLONE = "rclone"
RCLONE_SYNC = "/usr/local/bin/rclonesync"
RCLONE_SYNC_FILTERS_FILE = "/tmp/rclonesync-filters"
RCLONE_SYNC_FILTERS_FILE_CONTENTS = "- .rclonesync/"

DEBUG = False

PYTHON3 = False
if sys.version_info[0] == 3:
    PYTHON3 = True

#===================================================================

#-----[ Async Run ]-------------------------------------------------

#This class allows a command to be execute asynchronously.  Once
#the command exits, a signal is emitted with the exit code, and the
#contents of stdout and stderr.

class AsyncRun(GObject.GObject):
    __gsignals__ = {
        'process-done' : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_INT, GObject.TYPE_STRING, GObject.TYPE_STRING)),
    }

    def __init__(self):
        GObject.GObject.__init__(self)    

        self.p = None

    def run(self, cmd):
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE)

        if self.p:
            GLib.child_watch_add(self.p.pid,self._on_done)
            return True
        else:
            return False

    def _on_done(self, pid, retval, *argv):
        stdout, stderr = self.p.communicate()

        #This adds compatibility between python 2 and 3
        if (isinstance(stdout, bytes)):
                stdout = str(stdout.decode("utf-8"))
        if (isinstance(stderr, bytes)):
                stderr = str(stderr.decode("utf-8"))

        self.emit("process-done", retval, stdout, stderr)

#-----[ Async Execute Command ]-------------------------------------
class GAsyncSpawn(GObject.GObject):
    """ GObject class to wrap subprocess.Popen().
    
    Use:
        s = GAsyncSpawn()
        s.connect('process-done', mycallback)
        s.run(command)
            #command: list of strings
    """
    __gsignals__ = {
        'process-done' : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_INT, )),
        'stdout-data'  : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_STRING, )),
        'stderr-data'  : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_STRING, )),
    }
    def __init__(self):
        GObject.GObject.__init__(self)

        self._is_running = False
        self._p = None
        self._stdoutWatch = None
        self._stderrWatch = None

    def run(self, cmd):
        if self._is_running:
            raise Exception("Process is already running")
            return False

        #r  = GLib.spawn_async(cmd,flags=GLib.SPAWN_DO_NOT_REAP_CHILD, standard_output=True, standard_error=True)
        #self.pid, idin, idout, iderr = r
        #self.fout = os.fdopen(p.stdout, "r")
        #self.ferr = os.fdopen(p.stderr, "r")

        #Open the process with pipes for IO
        self._p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE)

        GLib.child_watch_add(self._p.pid,self._on_done)
        self._stdoutWatch = GLib.io_add_watch(self._p.stdout, GLib.IO_IN, self._on_stdout)
        self._stderrWatch = GLib.io_add_watch(self._p.stderr, GLib.IO_IN, self._on_stderr)

        self._is_running = True

        return True

    def stop(self):
        if self._is_running:
            self._p.terminate()

    def kill(self):
        if self._is_running:
            self._p.kill()

    def _on_done(self, pid, retval, *argv):
        #Ensure that the pipes are closed
        self._p.stdout.close()
        self._p.stderr.close()

        #These lines were added to prevent high CPU usage after command has exited
        GLib.source_remove(self._stdoutWatch)
        GLib.source_remove(self._stderrWatch)

        #Cleanup
        self._is_running = False
        self._p = None
        self._stdoutWatch = None
        self._stderrWatch = None

        self.emit("process-done", retval)     

    def _emit_std(self, name, value):
        self.emit(name+"-data", value)
    
    def _on_stdout(self, fobj, cond):
        if not fobj.closed:
            line = fobj.readline()
            if (isinstance(line, bytes)): #This adds compatibility between python 2 and 3
                line = str(line.decode("utf-8"))
            self._emit_std("stdout", line)
        return True #IO Watch will continue looking for more lines

    def _on_stderr(self, fobj, cond):
        if not fobj.closed:
            line = fobj.readline()
            if (isinstance(line, bytes)): #This adds compatibility between python 2 and 3
                line = str(line.decode("utf-8"))
            self._emit_std("stderr", line)
        return True #IO Watch will continue looking for more lines

    def is_running(self):
        return self._is_running

#-----[ FolderPath ]------------------------------------------------

#The FolderPath class represents a directory structure with an arbitrary root
#For example, a local filesystem path may be "/home/foo/bar" with "/" being the root
#An rclone path may be "remote:folder/foo" with "remote:" being the root

class FolderPath:
    @staticmethod
    def from_string(root, string):
        p = FolderPath(root)
        p.currentPath = string.lstrip(root).split("/")
        return p

    def __init__(self, root):
        #root is a string defining the root of the path
        #e.g. on a Linux system the root is "/"
        #e.g. on an Rclone remote the root could be "foo:"
        self.root = str(root)
        
        #Separator between path elements (default = "/")
        self.separator = "/"

        self.currentPath = []
    
    def __str__(self):
        return self.root + self.separator.join(self.currentPath)

    def __len__(self):
        #Length will always be at least one because there will always be the root
        return len(self.currentPath) + 1
    
    def __eq__(self, other):
        return str(self) == str(other)

    def __ne__(self, other):
        return str(self) != str(other)

    def append(self, part):
        self.currentPath.append(str(part))

    #Go back num number of directories in the path
    def back(self, num=1):
        if num > len(self.currentPath):
            num = len(self.currentPath)

        self.currentPath = self.currentPath[:-num]

    def at(self, i):
        if i == 0:
            return self.root
        elif (i > 0) and (i <= len(self.currentPath)):
            return self.currentPath[i - 1]
        else:
            return None

#-----[ PathBrowserProvider Interface ]----------------------------

#The PathBrowserProvider interface is inherited by all implementations of path browsers
#A path browser providers the folder structure of a path in a given file system

class PathBrowserProvider(GObject.GObject):
    __metaclass__ = ABCMeta

    __gsignals__ = {
        'get-path-contents-done' : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_PYOBJECT, GObject.TYPE_PYOBJECT)),
        'mkdir-done': (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_PYOBJECT, GObject.TYPE_BOOLEAN)), #True = Success, False = Failure
    }

    def __init__(self):
        GObject.GObject.__init__(self)

    @abstractmethod
    def get_path_contents(self, path): raise NotImplementedError #Return a list of the folder names in the given path, should return immediately
    @abstractmethod
    def get_root_path(self): raise NotImplementedError #The root (i.e. lowest directory) of the directory structure
    @abstractmethod
    def get_preferred_path(self): raise NotImplementedError #The path that the GUI should start at
    @abstractmethod
    def mkdir(self, path): raise NotImplementedError #Create a new directory with the given path, should return immediately
    @abstractmethod
    def error(self): raise NotImplementedError #Returns a string explaining the last error that occurred

#-----[ Debug PathBrowserProvider ]--------------------------------

#An example of a path browser provider implementation

class DebugPathBrowserProvider(PathBrowserProvider):
    def __init__(self):
        self.structure = {
            "/": {
                "foo": None,
                "bar": {
                    "path1": None,
                    "path2": None,
                    "path3": None
                }
            }
        }

    def get_path_contents(self, path):
        structurePart = self.structure

        for i in range(0, len(path)):
            pathPart = path.at(i)
            if pathPart in structurePart:
                structurePart = structurePart[pathPart]
            else:
                #Path does not exist so return empty contents
                return []

        #Return the contents of the current structure path
        if structurePart:
            return structurePart.keys()
        else:
            return []

    def get_root_path(self):
        return FolderPath("/")

    def get_preferred_path(self):
        fp = self.get_root_path()
        fp.append("bar")

        return fp

#-----[ Local Filesystem Path Browser Provider ]--------------------
class LocalPathBrowserProvider(PathBrowserProvider):
    def __init__(self):
        PathBrowserProvider.__init__(self)

    def get_path_contents(self, path):
        #This will "schedule" the internal function to be called later
        ret = GObject.timeout_add(0, self._get_path_contents, path)
        return ret >= 0

    def _get_path_contents(self, path):
        pathstr = str(path)
        #Emit the "done" signal
        self.emit('get-path-contents-done', path, [dI for dI in os.listdir(pathstr) if os.path.isdir(os.path.join(pathstr,dI))])

    def get_root_path(self):
        return FolderPath("/")

    def get_preferred_path(self):
        return FolderPath.from_string("/", os.path.expanduser("~"))

    def mkdir(self, path):
        #This will "schedule" the internal function to be called later
        ret = GObject.timeout_add(0, self._mkdir, path)
        return ret >= 0
    
    def _mkdir(self, path):
        success = False

        try:
            os.mkdir(str(path))
            success = True
        except Exception as e:
            success = False

        self.emit('mkdir-done', path, success)

#-----[ Rclone Path Browser Provider ]------------------------------
class RclonePathBrowserProvider(PathBrowserProvider):
    def __init__(self, remote):
        PathBrowserProvider.__init__(self)

        self.remote = remote
        self.run1 = AsyncRun()
        self.run2 = AsyncRun()
        self.run1.connect('process-done', self._on_run1_done)
        self.run2.connect('process-done', self._on_run2_done)

        self.getPathContentsPath = None
        self.mkdirPath = None

        self.lastError = ""

    def get_path_contents(self, path):
        if not self.getPathContentsPath:
            self.getPathContentsPath = path

            #Run the rclone "list directories" command with a 5 second timeout
            #The lsf command was added in rclone 1.48 and outputs files/folders in an easy-to-parse format
            try:
                return self.run1.run([RCLONE,'lsf',"--contimeout=5s","--dirs-only",str(path)])
            except Exception as e:
                self.lastError = str(e)
                return False
        else:
            return False
        

    def _on_run1_done(self, sender, retval, stdout, stderr):
        dirs = None

        if retval == 0: #Success
            listing = stdout.splitlines()
            dirs = []
            for l in listing:
                if (isinstance(l, bytes)): #This adds compatibility between python 2 and 3
                    l = str(l.decode("utf-8"))
                dirs.append(l[:-1])
        else:
            self.lastError = stderr

        self.emit('get-path-contents-done', self.getPathContentsPath, dirs)

        #Reset "flag" to allow function to be called again
        self.getPathContentsPath = None

    def get_root_path(self):
        return FolderPath(self.remote + ":")

    def get_preferred_path(self):
        return self.get_root_path()

    def mkdir(self, path):
        if not self.mkdirPath:
            self.mkdirPath = path

            try:
                #Run the rclone "mkdir" command with a 5 second timeout
                return self.run2.run([RCLONE,'mkdir',"--contimeout=5s",str(path)])
            except Exception as e:
                return False
        else:
            return False

    def _on_run2_done(self, sender, retval, stdout, stderr):
        if retval != 0:
            self.lastError = stderr

        self.emit('mkdir-done', self.mkdirPath, retval == 0)

        self.mkdirPath = None

    def error(self):
        return self.lastError

#-----[ Rclone Remote Browser Widget ]------------------------------

#This widget uses a TreeView as a list to browse through a directory 
#structure using a PathBrowserProvider implementation.

class PathBrowserWidget(Gtk.VBox):
    __gsignals__ = {
        'valid-changed': (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
                            (GObject.TYPE_BOOLEAN, )),
    }

    def __init__(self, parentWindow):
        #Call parent constructor and set to VERTICAL orientation
        Gtk.VBox.__init__(self, False, 5)

        self.pathProvider = None
        self.currentPath = None #Holds the path currently displayed
        self.currentPathValid = False

        self.handler1 = None
        self.handler2 = None
        self.parentWindow = parentWindow

        #Spinner shows current opeation status
        self.spinner = Gtk.Spinner()
    
        #Label shows the current path
        topBox = Gtk.HBox()
        self.lblPath = Gtk.Label()
        #self.btnNewFolder = Gtk.Button("New Folder")
        self.btnNewFolder = Gtk.Button.new_from_icon_name("folder-new", Gtk.IconSize.BUTTON)
        self.btnNewFolder.set_tooltip_text("Create a new folder in the current directory")
        self.btnNewFolder.connect("pressed", self.on_btnnewfolder_pressed)
        
        topBox.pack_start(self.spinner, False, True, 5)
        topBox.pack_start(self.lblPath, True, True, 5)
        topBox.pack_start(self.btnNewFolder, False, False, 5)

        #Columns: [Folder Name (str)]
        self.browserList = Gtk.ListStore(str)

        #Tree view shows current folder's contents
        #Used as a list view
        #Element 0 is always "./" -> go back one folder
        treeBrowserWindow = Gtk.ScrolledWindow()
        self.treeBrowser = Gtk.TreeView()
        self.treeBrowser.set_model(self.browserList)
        cell = Gtk.CellRendererText()
        col = Gtk.TreeViewColumn("Folder", cell, text=0) #Use a Text renderer and the 1st column from the model
        self.treeBrowser.append_column(col)
        #self.treeBrowser.set_mode(Gtk.SelectionMode.SINGLE)
        treeBrowserWindow.add(self.treeBrowser)

        self.treeBrowser.connect("row-activated", self.on_treebrowser_activated)

        #Text box allows the path to be given a custom label
        box = Gtk.Box(Gtk.Orientation.HORIZONTAL, 5)
        l = Gtk.Label("Label:")
        self.txtLabel = Gtk.Entry()
        self.txtLabel.set_tooltip_text("Provide a custom label for this sync location to be shown in the file manager's context menu")
        box.pack_start(l, False, False, 5)
        box.pack_start(self.txtLabel, True, True, 5)

        #Create main layout
        self.pack_start(topBox, False, True, 5)
        self.pack_start(treeBrowserWindow, True, True, 5)
        self.pack_start(box, False, True, 5)

    def _set_valid(self, valid):
        if self.currentPathValid != valid:
            self.currentPathValid = valid
            self.emit("valid-changed", valid)

    def get_selected_path(self):
        if self.currentPathValid: return (self.currentPath, self.txtLabel.get_text())
        else: return (None, None)

    def set_path_provider(self, provider):
        #Reset
        self.lblPath.set_text("")
        self.txtLabel.set_text("")
        self.currentPath = None
        self.browserList.clear()

        #print PLUGIN_NAME,": Setting path provider"

        #Disconnect signals from previous path provider
        if self.pathProvider:
            if self.handler1: self.pathProvider.disconnect(self.handler1)
            if self.handler2: self.pathProvider.disconnect(self.handler2)

            self.handler1 = None
            self.handler2 = None

        if provider:
            self.pathProvider = provider

            #Connect signals
            self.handler1 = self.pathProvider.connect('get-path-contents-done', self._on_path_provider_get_path_contents_done)
            self.handler2 = self.pathProvider.connect('mkdir-done', self._on_path_provider_mkdir_done)
            
            #Start at preferred path
            self.currentPath = self.pathProvider.get_preferred_path()
            self.display_path(self.currentPath)

    def display_path(self, path):
        if path and self.pathProvider:
            #Clear the current path's contents
            self.browserList.clear()

            self.lblPath.set_text(str(path))
            self.txtLabel.set_text(str(path)) #By default, the selected path is labeled the same as the full path
            self._set_valid(False)

            self.spinner.start()
            res = self.pathProvider.get_path_contents(path)
            if not res: self.__path_provider_get_path_contents_done([])

    def on_treebrowser_activated(self, widget, row, col):
        if row:
            folder = str(self.browserList[row][0])
            if folder == "../":
                #Go back one directory
                self.currentPath.back()
            else:
                #Go to the selected sub-directory
                self.currentPath.append(folder)
                
            self.display_path(self.currentPath)

    def on_btnnewfolder_pressed(self, button):
        d = StringInputDialog(self.parentWindow, "New Folder", "Please enter a name for the new folder")
        r = d.run()
        d.hide()

        if r == Gtk.ResponseType.OK:
            #Get the name the user typed and construct a new path
            folder_name = d.get_string()
            folder_path = copy.deepcopy(self.currentPath)
            folder_path.append(folder_name)

            if self.pathProvider:
                #Use the path provider to make the new folder
                self.spinner.start()
                res = self.pathProvider.mkdir(folder_path)
                if not res: self.__path_provider_mkdir_done(False)
                    
        d.destroy()

    def _on_path_provider_get_path_contents_done(self, sender, path, contents):
        self.spinner.stop()

        #First element goes back to previous folder
        if path != self.pathProvider.get_root_path():
            self.browserList.append(["../"])
        if not (contents is None):
            self._set_valid(True)

            for c in contents:
                self.browserList.append([str(c)])
        else: #Some error
            md = Gtk.MessageDialog(self.parentWindow, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "An error occurred")
            md.format_secondary_text(self.pathProvider.error())
            md.run()
            md.destroy()

    def _on_path_provider_mkdir_done(self, sender, path, success):
        self.spinner.stop()

        if success:
            #Go to the newly created folder
            self.currentPath = path
            self.display_path(path)
        else:
            md = Gtk.MessageDialog(self.parentWindow, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Unable to create a new directory")
            md.format_secondary_text(self.pathProvider.error())
            md.run()
            md.destroy()

#-----[ String Input Dialog ]---------------------------------------
class StringInputDialog(Gtk.Dialog):
    def __init__(self, parent, title="Input Required", prompt="Enter Input", buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)):
        Gtk.Dialog.__init__(self, title, parent, 0, buttons)

        self.set_default_size(300, 150)

        self.lblPrompt = Gtk.Label(prompt)
        self.txtString = Gtk.Entry()

        box = self.get_content_area()
        box.pack_start(self.lblPrompt, True, True, 5)
        box.pack_start(self.txtString, False, True, 5)

        self.show_all()

    def get_string(self):
        return self.txtString.get_text()  

#-----[ Path Selector Dialog ]--------------------------------------

#This dialog allows the user to select a new (i.e. other) path to sync
#from any of the configured rclone remotes or the Local Filesystem.

class NemoRcloneSyncProviderDialog(Gtk.Dialog):
    def __init__(self, parent):
        Gtk.Dialog.__init__(self, "Choose Other Sync Path", parent, 0, None)

        self.set_default_size(500, 450)
        #self.set_keep_above(True)

        self.pathProviders = {}

        #Remote selection
        scrolledWindow = Gtk.ScrolledWindow()
        self.remotesBox = Gtk.HBox()
        scrolledWindow.add_with_viewport(self.remotesBox)

        self.populate_remotes()

        #Separator
        sep1 = Gtk.HSeparator()

        #Path browser
        self.pathBrowserWidget = PathBrowserWidget(self)
        self.pathBrowserWidget.connect("valid-changed", self._on_valid_changed)

        box = self.get_content_area()
        box.pack_start(scrolledWindow, False, True, 5)
        box.pack_start(sep1, False, True, 5)
        box.pack_start(self.pathBrowserWidget, True, True, 5)

        self.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        self.btnOk = self.add_button(Gtk.STOCK_OK, Gtk.ResponseType.OK)
        self.btnOk.set_sensitive(False) #Disabled by default

        self.show_all()

    def _on_valid_changed(self, sender, valid):
        #Disable/Enable the OK button depending on if the selected path is valid
        self.btnOk.set_sensitive(valid)

    def get_selected_path(self):
        return self.pathBrowserWidget.get_selected_path()

    def populate_remotes(self):
        #Local filesystem
        #btn = Gtk.Button("Example")
        #self.pathProviders[btn.get_label()] = DebugPathBrowserProvider()
        #btn.connect("clicked", self.on_remotebutton_clicked)
        #self.remotesBox.add(btn)

        btn = Gtk.Button("Local Filesystem")
        self.pathProviders[btn.get_label()] = LocalPathBrowserProvider()
        btn.connect("clicked", self.on_remotebutton_clicked)
        self.remotesBox.add(btn)

        #Rclone
        rclone_remotes = self.rclone_get_remotes()
        for r in rclone_remotes:
            btn = Gtk.Button(r)
            self.pathProviders[btn.get_label()] = RclonePathBrowserProvider(r)
            btn.connect("clicked", self.on_remotebutton_clicked)
            self.remotesBox.add(btn)

    def on_remotebutton_clicked(self, button):
        if button:
            if button.get_label() in self.pathProviders:
                provider = self.pathProviders[button.get_label()]
                self.pathBrowserWidget.set_path_provider(provider)

    def rclone_get_remotes(self):
        out = subprocess.Popen([RCLONE,'listremotes'], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        stdout,stderr = out.communicate()

        if out.returncode == 0: #Success
            remotes = stdout.splitlines()
            remotes_out = []
            for r in remotes:
                if (isinstance(r, bytes)): #This adds compatibility between python 2 and 3
                    r = str(r.decode("utf-8"))
                if r[-1] == ":":
                    remotes_out.append(r[:-1])
            return remotes_out
        else:
            return []

#-----[ Sync Status Dialog ]---------------------------------------

#This dialog is simply a text area to display the real-time outout
#of the rclonesync command.  Once the command has completed, the OK
#button is enabled to allow the dialog to be closed.

class RcloneSyncStatusDialog(Gtk.Dialog):
    def __init__(self, parent):
        Gtk.Dialog.__init__(self, "Syncing...", parent, 0)

        self.set_default_size(800, 500)
        #self.set_keep_above(True)

        scrolledWindow = Gtk.ScrolledWindow()
        self.buffer = Gtk.TextBuffer()
        self.textView = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False, monospace=True)
        scrolledWindow.add(self.textView)

        self.btnOk = Gtk.Button("Ok", sensitive=False)
        self.btnOk.connect("pressed", self.on_btnok_pressed)

        box = self.get_content_area()
        box.pack_start(scrolledWindow, True, True, 5)
        box.pack_start(self.btnOk, False, True, 5)

        self.show_all()

    def print_line(self, line):
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, line)

    def set_ok_enabled(self, enabled):
        self.btnOk.set_sensitive(enabled)

    def on_btnok_pressed(self, widget):
        self.hide()

#-----[ Rclone Config Dialog ]---------------------------------------



class RcloneConfigDialog(Gtk.Dialog):
    def __init__(self, parent):
        Gtk.Dialog.__init__(self, "Configure Rclone", parent, 0)

        self.set_default_size(800, 500)

        #Command spawner
        self._spawn = GAsyncSpawn()
        self._spawn.connect("process-done", self._on_process_done)
        self._spawn.connect("stdout-data", self._on_stdout_data)
        self._spawn.connect("stderr-data", self._on_stdout_data)

        #Debug output terminal
        scrolledWindow = Gtk.ScrolledWindow()
        self.buffer = Gtk.TextBuffer()
        self.textView = Gtk.TextView(buffer=self.buffer, editable=False, cursor_visible=False, monospace=True)
        scrolledWindow.add(self.textView)

        #Control buttons
        boxControls = Gtk.HBox()
        self.btnStart = Gtk.Button("Start Web GUI", sensitive=True)
        self.btnStart.connect("pressed", self._on_btnstart_pressed)
        self.btnStop = Gtk.Button("Stop Web GUI", sensitive=False)
        self.btnStop.connect("pressed", self._on_btnstop_pressed)
        boxControls.pack_start(self.btnStart, True, True, 5)
        boxControls.pack_start(self.btnStop, True, True, 5)

        box = self.get_content_area()
        box.pack_start(scrolledWindow, True, True, 5)
        box.pack_start(boxControls, False, True, 5)

        self._btnDone = self.add_button("Done", Gtk.ResponseType.CLOSE)

        self.show_all()

        self._print_line("Rclone can be configured via an integrated web GUI.\nPress the 'Start Web GUI' button to launch this GUI.\nYour web browser should automatically be opened.\nAfter you are done and have closed your web browser, press 'Stop Web GUI'.\n")

    def _print_line(self, line):
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, line)

    def _on_btnstart_pressed(self, sender):
        if self._spawn.is_running():
            #Ensure the correct buttons are enabled
            self.btnStart.set_sensitive(False)
            self.btnStop.set_sensitive(True)
            return

        cmd = [RCLONE, "rcd", "--rc-web-gui"]
        self._print_line("RUNNING: " + " ".join(cmd) + "\n\n")

        try:
            self._spawn.run(cmd)

            self.btnStart.set_sensitive(False)
            self.btnStop.set_sensitive(True)
            self._btnDone.set_sensitive(False)
        except Exception as e:
            self._print_line("ERROR: " + str(e))

    def _on_btnstop_pressed(self, sender):
        if not self._spawn.is_running():
            #Ensure the correct buttons are enabled
            self.btnStart.set_sensitive(True)
            self.btnStop.set_sensitive(False)
            return

        self._spawn.stop()
    
    def _on_process_done(self, sender, retval):
        self._print_line("Process exited with return value " + str(retval) + "\n")

        self.btnStart.set_sensitive(True)
        self.btnStop.set_sensitive(False)
        self._btnDone.set_sensitive(True)
    
    def _on_stdout_data(self, sender, line):
        self._print_line(line)

#-----[ Nemo Plugin ]-----------------------------------------------
class NemoRcloneSyncProvider(GObject.GObject, Nemo.MenuProvider, Nemo.NameAndDescProvider):
    def __init__(self):
        GObject.GObject.__init__(self)

        self.last_dir = None
        self.meta_object_cache = {}
        self.syncDialog = None
        self.currentSync = [None, None]

        #Setup the Async command executer
        self.spawn = GAsyncSpawn()
        self.spawn.connect("process-done", self.on_process_done)
        self.spawn.connect("stdout-data", self.on_stdout_data)
        self.spawn.connect("stderr-data", self.on_stdout_data)

    #-----[ Event Callbacks ]--------------------------------------------------
    def on_menu_other_activated(self, menu, parent, folder):
        #Display the remote folder selection dialog
        dialog = NemoRcloneSyncProviderDialog(parent)
        resp = dialog.run()

        #A path was selected
        if resp == Gtk.ResponseType.OK:
            path, label = dialog.get_selected_path()

            #Update the metadata file to save this path for future use
            if not self.meta_object_cache:
                self.meta_object_cache = DEFAULT_META_OBJECT
            if not "places" in self.meta_object_cache:
                self.meta_object_cache["places"] = []

            self.meta_object_cache["places"].append({"label":label, "path":str(path)})
            self.write_meta_file(folder, self.meta_object_cache)

            #Start a sync using the selected remote
            self.on_sync_requested(None, parent, str(folder), str(path), self.meta_object_cache["first_sync"])

        dialog.destroy()
        return

    def _on_config_rclone_menuitem_activated(self, menu, parent):
        dialog = RcloneConfigDialog(parent)
        resp = dialog.run()

        dialog.destroy()
        return

    def on_sync_requested(self, menu, parent, folder1, folder2, first_sync=False):
        if DEBUG: print(PLUGIN_NAME,":: Syncing:",folder1,"and",folder2)

        #Create a temporary filters_file for rclonesync if one does not already exist
        if not os.path.isfile(RCLONE_SYNC_FILTERS_FILE):
            with open(RCLONE_SYNC_FILTERS_FILE, "w+") as f:
                f.write(RCLONE_SYNC_FILTERS_FILE_CONTENTS)

        #rclonesync command and arguments
        args = [RCLONE_SYNC]
        if first_sync:
            args.append("--first-sync")
        args.append("--verbose")
        args.append("--filters-file")
        args.append(RCLONE_SYNC_FILTERS_FILE)
        args.append(folder2)
        args.append(folder1)

        self.currentSync[0] = folder1
        self.currentSync[1] = folder2

        #Open dialog to display real-time command status
        if self.syncDialog:
            self.syncDialog.destroy()
        self.syncDialog = RcloneSyncStatusDialog(parent)
        self.syncDialog.show()

        self.syncDialog.print_line("RUNNING: " + " ".join(args) + "\n\n")

        #Execute rclonesync
        self.spawn.run(args)

    def on_process_done(self, sender, retval):
        if DEBUG: print(PLUGIN_NAME, ":: rclonesync has finished with the return value", retval)

        self.syncDialog.print_line("DONE!\n")
        self.syncDialog.set_ok_enabled(True)

        if retval == 0:
            #Update the metadata IF a first-sync was just performed
            if self.meta_object_cache["first_sync"]:
                self.meta_object_cache["first_sync"] = False
                self.write_meta_file(self.currentSync[0], self.meta_object_cache)       

    def on_stdout_data(self, sender, line):
        #Append command output to the popup dialog
        self.syncDialog.print_line(line)
    def on_stderr_data(self, sender, line):
        pass
        #print PLUGIN_NAME,":: rclonesync produced an error:",line

    #-----[ Utilities ]----------------------------------------------------------
    def get_system_name(self) :
        hostname = socket.gethostname() #Get the computer hostname
        
        return re.sub("[^A-Za-z0-9]+","",hostname) #Strip special characters

    def get_make_meta_dir(self, folder, create=True):
        meta_dir = folder + "/" + META_DIR

        if create:
            if not os.path.exists(meta_dir): #Create folder if not exists
                os.makedirs(meta_dir)

        return meta_dir

    def read_meta_file(self, folder):
        meta_filename = self.get_make_meta_dir(folder, False) + "/" + META_FILE_PREFIX + "." + self.get_system_name() #This is the sync metadata file

        try:
            f = open(meta_filename, "r") #Attempt to open the file
            json_data = json.load(f) #Attempt to read JSON data from file
            f.close()
            
            return json_data

            #if json_data['version'] == VERSION:
            #    return json_data
            #else:
            #    return DEFAULT_META_OBJECT
        except Exception as e:
            if DEBUG: print(PLUGIN_NAME,"::Error reading meta file at:",meta_filename,"->",str(e))
            return DEFAULT_META_OBJECT

    def write_meta_file(self, folder, meta_object):
        meta_filename = self.get_make_meta_dir(folder) + "/" + META_FILE_PREFIX + "." + self.get_system_name() #This is the sync metadata file

        try:
            f = open(meta_filename, "w")
            json.dump(meta_object, f)
            f.close()
        except Exception as e:
            if DEBUG: print(PLUGIN_NAME,"::",str(e))

    #-----[ Nemo Hooks ]----------------------------------------------------------
    def get_file_items(self, window, files):
        if len(files) != 1: #Only allow for a single folder selection
            return

        folder = files[0]
        if not folder.is_directory(): #Only allow on folders
            return

        #Get the full system path of the selected folder
        folder_uri = urllib.parse.unquote(folder.get_uri()[7:]) if PYTHON3 else urllib.unquote(folder.get_uri()[7:])
        folder_name = os.path.basename(os.path.normpath(folder_uri))

        #Prevents recursion issues
        if folder_name == META_DIR:
            return

        top_menuitem = Nemo.MenuItem(name='NemoRcloneSyncProvider::Sync',
                                     label='Sync',
                                     tip='Perform an rclone sync of this folder to a remote',
                                     icon='network-transmit-receive') #possible icons = "add", "network-transmit-receive"

        submenu = Nemo.Menu()
        top_menuitem.set_submenu(submenu)

        #Was the same folder opened again?
        #Prevents the metadata file from being read multiple times
        if folder_uri != self.last_dir:
            self.meta_object_cache = self.read_meta_file(folder_uri) #Get the sync metadata (if any) for this folder
            self.last_dir = folder_uri
        
        if "places" in self.meta_object_cache:
            places = self.meta_object_cache["places"]
            for p in places:
                #Create a new menu item for every remote path
                if ("label" in p) and ("path" in p):
                    sub_menuitem = Nemo.MenuItem(name=PLUGIN_NAME + "::Place-" + p["label"],
                                     label=p["label"],
                                     tip='Sync to this remote directory',
                                     icon='folder')

                    sub_menuitem.connect('activate', self.on_sync_requested, window, str(folder_uri), str(p["path"]), self.meta_object_cache["first_sync"])

                    submenu.append_item(sub_menuitem)

        #Append a separator
        sum_menuitem_separator = Nemo.MenuItem.new_separator(PLUGIN_NAME + "::Other_separator")
        submenu.append_item(sum_menuitem_separator)

        #Append the "other" option to the menu
        sub_menuitem = Nemo.MenuItem(name=PLUGIN_NAME + "::Other",
                                     label='Other...',
                                     tip='Choose a destination directory not listed here',
                                     icon='folder-saved-search')
        sub_menuitem.connect('activate', self.on_menu_other_activated, window, folder_uri)
        submenu.append_item(sub_menuitem)

        #Append the "config rclone" option to the menu
        config_rclone_menuitem = Nemo.MenuItem(name=PLUGIN_NAME + "::ConfigureRclone",
                                     label='Configure Rclone...',
                                     tip='Open the Rclone configuration web GUI',
                                     icon='preferences-other')
        config_rclone_menuitem.connect('activate', self._on_config_rclone_menuitem_activated, window)
        submenu.append_item(config_rclone_menuitem)

        return top_menuitem,

    def get_name_and_desc(self):
        return [PLUGIN_TITLE + ":::Sync a folder to a remote location via rclone"]
