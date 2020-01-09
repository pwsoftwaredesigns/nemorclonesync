#-----[ Includes ]-------------------------------------------------
from gi.repository import Nemo, GObject, Gtk, GLib
import json
import urllib
import socket
import re
import os
import subprocess
import copy
from abc import ABCMeta, abstractmethod

#-----[ Constants ]------------------------------------------------
PLUGIN_NAME = "NemoRcloneSyncProvider"
PLUGIN_TITLE = "Nemo Rclone Sync"
VERSION = 1
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

DEBUG = True

#===================================================================

#-----[ Async Execute Command ]-------------------------------------
class GAsyncSpawn(GObject.GObject):
    """ GObject class to wrap GLib.spawn_async().
    
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

    def run(self, cmd):
        #r  = GLib.spawn_async(cmd,flags=GLib.SPAWN_DO_NOT_REAP_CHILD, standard_output=True, standard_error=True)
        #self.pid, idin, idout, iderr = r
        #self.fout = os.fdopen(p.stdout, "r")
        #self.ferr = os.fdopen(p.stderr, "r")

        #Open the process with pipes for IO
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,stderr=subprocess.PIPE)

        self.pid = p.pid
        self.fout = p.stdout
        self.ferr = p.stderr

        GLib.child_watch_add(self.pid,self._on_done)
        self.foutWatch = GLib.io_add_watch(self.fout, GLib.IO_IN, self._on_stdout)
        self.ferrWatch = GLib.io_add_watch(self.ferr, GLib.IO_IN, self._on_stderr)
        return self.pid

    def _on_done(self, pid, retval, *argv):
        #Ensure that the pipes are closed
        self.fout.close()
        self.ferr.close()

        #These lines were added to prevent high CPU usage after command has exited
        GLib.source_remove(self.foutWatch)
        GLib.source_remove(self.ferrWatch)

        self.emit("process-done", retval)

    def _emit_std(self, name, value):
        self.emit(name+"-data", value)
    
    def _on_stdout(self, fobj, cond):
        if not fobj.closed:
            self._emit_std("stdout", fobj.readline())
        return True #IO Watch will continue looking for more lines

    def _on_stderr(self, fobj, cond):
        if not fobj.closed:
            self._emit_std("stderr", fobj.readline())
        return True #IO Watch will continue looking for more lines

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

class PathBrowserProvider:
    __metaclass__ = ABCMeta

    #__gsignals__ = {
    #    'got-path-contents' : (GObject.SIGNAL_RUN_LAST, GObject.TYPE_NONE,
    #                        (GObject.TYPE_INT, )),
    #}

    @abstractmethod
    def get_path_contents(self, path): raise NotImplementedError #Return a list of the folder names in the given path
    @abstractmethod
    def get_root_path(self): raise NotImplementedError #The root (i.e. lowest directory) of the directory structure
    @abstractmethod
    def get_preferred_path(self): raise NotImplementedError #The path that the GUI should start at
    @abstractmethod
    def mkdir(self, path): raise NotImplementedError #Create a new directory with the given path

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
        pass #Nothing to init

    def get_path_contents(self, path):
        pathstr = str(path)
        return [dI for dI in os.listdir(pathstr) if os.path.isdir(os.path.join(pathstr,dI))]

    def get_root_path(self):
        return FolderPath("/")

    def get_preferred_path(self):
        return FolderPath.from_string("/", os.path.expanduser("~"))

    def mkdir(self, path):
        try:
            os.mkdir(str(path))
            return True
        except Exception as e:
            return False

#-----[ Rclone Path Browser Provider ]------------------------------
class RclonePathBrowserProvider(PathBrowserProvider):
    def __init__(self, remote):
        self.remote = remote

    def get_path_contents(self, path):
        #Run the rclone "list directories" command with a 5 second timeout
        out = subprocess.Popen([RCLONE,'lsd',"--contimeout=5s",str(path)], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        stdout,stderr = out.communicate()

        if out.returncode == 0: #Success
            listing = stdout.splitlines()
            dirs = []
            for l in listing:
                s = re.search(r'.*-1\s(.*)', l)
                dirs.append(s.group(1))

            return dirs
        else: #Error
            return None #Return empty contents

    def get_root_path(self):
        return FolderPath(self.remote + ":")

    def get_preferred_path(self):
        return self.get_root_path()

    def mkdir(self, path):
        try:
            #Run the rclone "mkdir" command with a 5 second timeout
            out = subprocess.Popen([RCLONE,'mkdir',"--contimeout=5s",str(path)], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            stdout,stderr = out.communicate()

            if out.returncode == 0: #Success
                return True
            else:
                return False
        except Exception as e:
            return False

#-----[ Rclone Remote Browser Widget ]------------------------------

#This widget uses a TreeView as a list to browse through a directory 
#structure using a PathBrowserProvider implementation.

class PathBrowserWidget(Gtk.VBox):
    def __init__(self):
        #Call parent constructor and set to VERTICAL orientation
        Gtk.VBox.__init__(self, False, 5)

        self.pathProvider = None
        self.currentPath = None #Holds the path currently displayed

        #Label shows the current path
        topBox = Gtk.HBox()
        self.lblPath = Gtk.Label()
        #self.btnNewFolder = Gtk.Button("New Folder")
        self.btnNewFolder = Gtk.Button.new_from_icon_name("folder-new", Gtk.IconSize.BUTTON)
        self.btnNewFolder.set_tooltip_text("Create a new folder in the current directory")
        self.btnNewFolder.connect("pressed", self.on_btnnewfolder_pressed)
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

    def get_selected_path(self):
        return (self.currentPath, self.txtLabel.get_text())

    def set_path_provider(self, provider):
        #Reset
        self.lblPath.set_text("")
        self.txtLabel.set_text("")
        self.currentPath = None
        self.browserList.clear()

        #print PLUGIN_NAME,": Setting path provider"

        if provider:
            self.pathProvider = provider
            
            #Start at preferred path
            self.currentPath = self.pathProvider.get_preferred_path()
            self.display_path(self.currentPath)

    def display_path(self, path):
        if path and self.pathProvider:
            #Clear the current path's contents
            self.browserList.clear()

            self.lblPath.set_text(str(path))
            self.txtLabel.set_text(str(path)) #By default, the selected path is labeled the same as the full path

            contents = self.pathProvider.get_path_contents(path)
            #print PLUGIN_NAME,": Path contents =",str(contents)

            #First element goes back to previous folder
            if path != self.pathProvider.get_root_path():
                self.browserList.append(["../"])
            if contents:
                for c in contents:
                    self.browserList.append([str(c)])

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
        d = StringInputDialog(None, "New Folder", "Please enter a name for the new folder")
        r = d.run()
        d.hide()

        if r == Gtk.ResponseType.OK:
            #Get the name the user typed and construct a new path
            folder_name = d.get_string()
            folder_path = copy.deepcopy(self.currentPath)
            folder_path.append(folder_name)

            if self.pathProvider:
                #Use the path provider to make the new folder
                res = self.pathProvider.mkdir(folder_path)
                if res:
                    #Go to the newly created folder
                    self.currentPath = folder_path
                    self.display_path(folder_path)
                else:
                    md = Gtk.MessageDialog(None, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Unable to create a new directory")
                    md.format_secondary_text("Perhaps the name you entered was invalid or you do not have write access for the current directory")
                    md.run()
                    md.destroy()
                    
        d.destroy()
            

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
        Gtk.Dialog.__init__(self, "Choose Other Sync Path", parent, 0, (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK))

        self.set_default_size(500, 450)

        self.pathProviders = {}

        #Remote selection
        scrolledWindow = Gtk.ScrolledWindow()
        self.remotesBox = Gtk.HBox()
        scrolledWindow.add_with_viewport(self.remotesBox)

        self.populate_remotes()

        #Separator
        sep1 = Gtk.HSeparator()

        #Path browser
        self.pathBrowserWidget = PathBrowserWidget()

        box = self.get_content_area()
        box.pack_start(scrolledWindow, False, True, 5)
        box.pack_start(sep1, False, True, 5)
        box.pack_start(self.pathBrowserWidget, True, True, 5)

        self.show_all()

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
            if self.pathProviders.has_key(button.get_label()):
                provider = self.pathProviders[button.get_label()]
                self.pathBrowserWidget.set_path_provider(provider)

    def rclone_get_remotes(self):
        out = subprocess.Popen([RCLONE,'listremotes'], stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        stdout,stderr = out.communicate()

        remotes = stdout.splitlines()
        return [x[:-1] for x in remotes]

#-----[ Sync Status Dialog ]---------------------------------------

#This dialog is simply a text area to display the real-time outout
#of the rclonesync command.  Once the command has completed, the OK
#button is enabled to allow the dialog to be closed.

class RcloneSyncStatusDialog(Gtk.Dialog):
    def __init__(self, parent):
        Gtk.Dialog.__init__(self, "Synching...", parent, 0)

        self.set_default_size(800, 500)

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
    def on_menu_other_activated(self, menu, folder):
        #Display the remote folder selection dialog
        dialog = NemoRcloneSyncProviderDialog(None)
        resp = dialog.run()

        #A path was selected
        if resp == Gtk.ResponseType.OK:
            path, label = dialog.get_selected_path()

            #Update the metadata file to save this path for future use
            if not self.meta_object_cache:
                self.meta_object_cache = DEFAULT_META_OBJECT
            if not self.meta_object_cache.has_key("places"):
                self.meta_object_cache["places"] = []

            self.meta_object_cache["places"].append({"label":label, "path":str(path)})
            self.write_meta_file(folder, self.meta_object_cache)

            #Start a sync using the selected remote
            self.on_sync_requested(None, str(folder), str(path))

        dialog.destroy()
        return

    def on_sync_requested(self, menu, folder1, folder2, first_sync=False):
        if DEBUG: print PLUGIN_NAME,":: Synching:",folder1,"and",folder2

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
        self.syncDialog = RcloneSyncStatusDialog(None)
        self.syncDialog.show()

        self.syncDialog.print_line("RUNNING: " + " ".join(args) + "\n\n")

        #Execute rclonesync
        self.spawn.run(args)

    def on_process_done(self, sender, retval):
        if DEBUG: print PLUGIN_NAME,":: rclonesync has finished with the return value", retval

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
            
            if json_data['version'] == VERSION:
                return json_data
            else:
                return DEFAULT_META_OBJECT
        except Exception as e:
            if DEBUG: print PLUGIN_NAME,"::Error reading meta file at:",meta_filename,"->",str(e)
            return DEFAULT_META_OBJECT

    def write_meta_file(self, folder, meta_object):
        meta_filename = self.get_make_meta_dir(folder) + "/" + META_FILE_PREFIX + "." + self.get_system_name() #This is the sync metadata file

        try:
            f = open(meta_filename, "w")
            json.dump(meta_object, f)
            f.close()
        except Exception as e:
            if DEBUG: print PLUGIN_NAME,"::",str(e)

    #-----[ Nemo Hooks ]----------------------------------------------------------
    def get_file_items(self, window, files):
        if len(files) != 1: #Only allow for a single folder selection
            return

        folder = files[0]
        if not folder.is_directory(): #Only allow on folders
            return

        #Get the full system path of the selected folder
        folder_uri = urllib.unquote(folder.get_uri()[7:])
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
        
        if self.meta_object_cache.has_key("places"):
            places = self.meta_object_cache["places"]
            for p in places:
                #Create a new menu item for every remote path
                if p.has_key("label") and p.has_key("path"):
                    sub_menuitem = Nemo.MenuItem(name=PLUGIN_NAME + "::Place-" + p["label"],
                                     label=p["label"],
                                     tip='Sync to this remote directory',
                                     icon='folder')

                    first_sync = self.meta_object_cache["first_sync"]
                    sub_menuitem.connect('activate', self.on_sync_requested, str(folder_uri), str(p["path"]), first_sync)

                    submenu.append_item(sub_menuitem)

        #Append a separator
        sum_menuitem_separator = Nemo.MenuItem.new_separator(PLUGIN_NAME + "::Other_separator")
        submenu.append_item(sum_menuitem_separator)

        #Append the "other" option to the menu
        sub_menuitem = Nemo.MenuItem(name=PLUGIN_NAME + "::Other",
                                     label='Other...',
                                     tip='Choose a destination directory not listed here',
                                     icon='folder-saved-search')
        sub_menuitem.connect('activate', self.on_menu_other_activated, folder_uri)
        submenu.append_item(sub_menuitem)

        return top_menuitem,

    def get_name_and_desc(self):
        return [PLUGIN_TITLE + ":::Sync a folder to a remote location via rclone"]
