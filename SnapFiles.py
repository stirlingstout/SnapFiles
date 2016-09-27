#!/usr/bin/python

"""
Based on Snap! extension to support Raspberry Pi -- server component,
this is a Snap! extension to allow the reading and writing of text files.

The original extension was copyright (C) 2014  Paul C. Brown <p_brown@gmx.com>.
This program produced by John Stout <cuspcomputers@gmail.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import http.server
import os
import getpass
import re
import socketserver
import urllib.request
import urllib.parse
import tempfile
import shutil
import sys
import argparse

VERSION = "V1.4"
DEFAULT_PORT = 7083                         # F+S (File System) in ASCII Decimal

f = tempfile.NamedTemporaryFile(mode="w+", suffix=".tmp", prefix="snap", delete=False)
TEMPORARY_FILE_NAME = f.name
f.close()

parser = argparse.ArgumentParser()
parser.add_argument("-t", "--trace", help="display tracing information", action="store_true")
parser.add_argument("-p", "--port", help="use a particular port", action="store", default=DEFAULT_PORT, type=int)
SHOW_TRACE = parser.parse_args().trace
PORT = parser.parse_args().port

# These three path functions should work on Mac, Windows, Linux (Raspberry Pi)
def HOMEPath():
    return os.path.expanduser("~") 

def documentsPath():
    """ This returns the Documents path, e.g., e.g., for logged on user john
    Mac /Users/john/Documents
    RPi /home/john/Documents
    Win C:\\Users\\john\\Documents """

    return os.path.join(HOMEPath(), "Documents" )

def snapFilesPath(username):
    """ Returns the document path with the username (if given) appended, e.g.,
    for logged on user john, but passed username=fred
    Mac /Users/john/Documents/SnapFiles/fred
    RPi /home/john/Documents/SnapFiles/fred
    Win C:\\Users\\john\\Documents\\SnapFiles\\fred
   
    If username is blank we get

    Mac /Users/john/Documents/SnapFiles/
    RPi /home/john/Documents/SnapFiles/
    Win C:\\Users\\john\\Documents\\SnapFiles\\
    
    but adding the filename (in filepath()) is done intelligently, so that the
    terminating / (or \\) isn't doubled.  
    """

    return os.path.join(documentsPath(), "SnapFiles", username)

def filepath(username, filename):
    """ Returns the absolute file path given a file name. Anything other than a filename
    and extension is removed from filename.
    """
    if username and not os.path.exists(snapFilesPath(username)):
        os.makedirs(snapFilesPath(username))

    return os.path.join(snapFilesPath(username), os.path.basename(filename))

def debug(message):
    global SHOW_TRACE
    if SHOW_TRACE:
        print(message)

class FileCache(dict):
    """ I maintain a cache of files, keyed by name (actually the full path so that two files with the 
    same name can be accessed as long as they are in different directories, although for security reasons
    currently only one path, to the user's home directory, is supported).
    
    In this way the name can be used instead of a file-handle or some such mechanism to access the file 
    and its contents (the first time I came across this method was when using awk).
   
    Initially I will just hold the object returned on an open, so including its read/write position.

    TODO: pass homepath into constructor
    """
    def closeAll(self):
        for file in self:
            self[file].close    
        # Trouble with doing self.close() is that we are iterating over a dictionary but the dictionary changes during the iteration
        #   because of the self.pop() so we close all the files in the iterator then clear the dictionary
        self.clear()

    def close(self, username, name):
        if filepath(username, name) in self:
            self[filepath(username, name)].close()
            self.pop(filepath(username, name), None)

    def truncate(self, username, name):
        if filepath(username, name) in self:
            self[filepath(username, name)].truncate()

    def file(self, username, name, mode=None):
        filename = filepath(username, name)
        if filename not in self:
            if not os.path.isfile(filename):
                f = open(filename, 'w')
                f.close()
            self[filename] = open(filename, "rb+")
        elif mode and self[filename].mode != mode:
            print("OPENING FILE IN MODE THAT ISN'T rb+")
            self[filename].close()
            self[filename] = open(filename, "rb+")
        return self[filename]

    def setPosition(self, username, name, toPosition, whence):
        f = self.file(username, name)
        self[filepath(username, name)].seek(toPosition, whence)

    def reset(self, username, name):
        self.setPosition(username, name, 0)

    def getPosition(self, username, name):
        return self.file(username, name).tell()

    def remove(self, username, name):
        self.close(username, name)
        try:
            os.remove(filepath(username, name))
            return (True, "")
        except OSError as e:
            return (False, e.strerror)
        except:
            return (False, "unknown error ")

    def rename(self, username, name, newname):
        self.close(username, name)
        self.close(username, newname) # It might be open, you never know! If it exists then we'll still get an error
        try:
            os.rename(filepath(username, name), filepath(username, newname))
            self.setPosition(username, newname, 0, 0)

            return (True, "")
        except OSError as e:
            return (False, e.strerror)
        except:
            return (False, "unknown error")

    def copyFile(self, username, fromFile, toFile):
        self.close(username, fromFile)
        self.close(username, toFile)

        try:
            shutil.copyfile(filepath(username, fromFile), filepath(username, toFile))   # Not sure this ever throws an exception
            return (True, "")
        except OSError as e:
            return (False, e.strerror)
        except:
            return (False, "unknown error")

class Responder():
    """ I hold a 'jump table' and act as a jump table handler. This is the 
    Pythonic way to do the equivalent of a case/switch statement: see
    https://docs.python.org/2/faq/design.html#why-isn-t-there-a-switch-or-case-statement-in-python
    
    The class is really being used as a holder for the code/data, not really using OOP.  It saves the long
       
       if command == "...":
       elif command == "...":
       elif command == "...":
       ...
       else:
       
    structure """
    def writeResult(data):
        """ Write data to the temporary file and return the name of the file.
        I wonder if we could hold the temporary file in the file cache and so
        just use one file instead of littering the place with files """
        f = open(TEMPORARY_FILE_NAME, "w+")
        f.writelines(data if type(data) == type([]) else str(data))                    
        f.close()
            
        return TEMPORARY_FILE_NAME
        
    def error(message):
        return Responder.writeResult("ERROR: " + message)
        
    def OK():
        return Responder.writeResult("OK")   
        
    def readLine(f):
        """ Read from f, byte at a time, until you get to os.linesep. On Windows then os.linesep is
        \r\n so we skip the \n as well as the \r """
        result = ""
        while True:
            ch = f.read(1).decode("utf-8")
            if ch == '':    # End of file
                return result
            if ch != os.linesep[0]:
                result += ch
            else:
                if len(os.linesep) > 1:
                    ch = f.read(1)
                return result 

    def readall(username, filename, data, parsedQuery):
        if filename:
            debug("read all from " + filename)
               
            f = files.file(username, filename)                # Why is files accessible? Shouldn't I have to label is global? 
            f.seek(0)
            lines = f.read().decode("utf-8")        # All these separate lines lines are for debugging, although putting it into 1 line stinks!
            lines = lines.replace(os.linesep, '\n')
            lines = lines.split('\n')[:-1]
            lines = [line + os.linesep[-1] for line in lines]
            if lines:                                                               # Otherwise you get an error reading from an empty file
                lines[-1] = lines[-1] if lines[-1][-1] != '\n' else lines[-1][:-1]  # The last line has a trailing \n removed if it ends in one!
            return Responder.writeResult(lines)      # Still not ideal as you get a blank element returned, but this might be to do with Snap!
        else:
            return Responder.error("no filename") 

    def append(username, filename, data, parsedQuery):
        if filename:
            if data:
                debug("append to " + filename + " data " + data )
               
                f = files.file(username, filename)
                f.seek(0, 2)        # Position to the end of the file
                r = bytes(data + os.linesep, "utf-8")

                f.write(bytes(data + os.linesep, "utf-8"))
                f.flush()

                # What do we return?
                return Responder.OK()
            else:
                return Responder.error("no data")
        else:
            return Responder.error("no filename")

    def read(username, filename, data, parsedQuery):
        if filename:
            if data:
                debug("read from " + filename + " mode " + data)
                if data == "nextline":
                    f = files.file(username, filename)
                    return Responder.writeResult(Responder.readLine(f)) # Need to read until we've read os.linesep (this is \r\n in Windows)
                elif data == "characters":
                    if "count" in parsedQuery:
                        count = parsedQuery["count"][0]
                        if count.isdigit():
                            debug("read from " + filename + " mode " + data + " count " + str(count))
                            result = files.file(username, filename).read(int(count)).decode("utf-8")
                            debug("result " + result)
                            return Responder.writeResult(result)
                        else:
                            return Responder.error("invalid count")
                    else:
                        return Responder.error("no count specified")
                else:
                    return Responder.error("invalid read mode specified")
            else:
                return Responder.error("no read mode specified")
        else:
            return Responder.error("no filename")

    def closeAll(username, filename, data, parsedQuery):
        files.closeAll()
        return Responder.OK()

    def atEnd(username, filename, data, parsedQuery):
        if filename:
            debug("at end of " + filename)
               
            f = files.file(username, filename, None)
                
            return Responder.writeResult(str(f.tell() == os.fstat(f.fileno()).st_size))
            # Above line to detect end of file from http://stackoverflow.com/questions/10140281/how-to-find-out-whether-a-file-is-at-its-eof+
        else:
            return Responder.error("no filename")

    def close(username, filename, data, parsedQuery):
        if filename:
            debug("close " + filename)
                
            files.close(username, filename)
            return Responder.OK()
            # close never gives an error
        else:
            return Responder.error("no filename")

    def truncate(username, filename, data, parsedQuery):
        if filename:
            debug("truncate " + filename)
            print("current location in " + filename + " = " + str(files.getPosition(username, filename)))  
            files.truncate(username, filename)    

            return Responder.OK()
            # truncate never gives an error
        else:
            return Responder.error("no filename")

    def setPosition(username, filename, data, parsedQuery):
        if filename:
            if data:
                debug("setposition of " + filename + " to " + data)
                if "relativeto" in parsedQuery:
                    relativeTo = parsedQuery["relativeto"][0]
                    if relativeTo == "start":
                        whence = 0
                    elif relativeTo == "current":
                        whence = 1
                    elif relativeTo == "end":
                        whence = 2
                    else:
                        whence = -1
                if whence != -1:
                    if data.isdigit() or (data[0] in ['+', '-'] and data[1:].isdigit()):
                        position = int(data)
                        files.setPosition(username, filename, position, whence)
                        return Responder.OK()
                    else:
                        return Responder.error("invalid position")
                else:
                    return Responder.error("invalid relativeto")
            else:
                return Responder.error("no data specified")
        else:
            return Responder.error("no filename")

    def position(username, filename, data, parsedQuery):
        if filename:
            debug("get position " + filename + "=" + str(files.getPosition(username, filename)))
            return Responder.writeResult(str(files.getPosition(username, filename)))
        else:
            return Responder.error("no filename")

    def exists(username, filename, data, parsedQuery):
        if filename:
            debug("exists " + filename)

            return Responder.writeResult(str(os.path.isfile(filepath(username, filename))))
        else:
            return Responder.error("no filename")

    def delete(username, filename, data, parsedQuery):
        if filename:
            debug("delete " + filename)

            result = files.remove(username, filename)
            if result[0]:
                return Responder.OK()
            else:
                return Responder.error("file cannot be removed" + " (" + result[1] + ")")
        else:
            return Responder.error("no filename")

    def rename(username, filename, data, parsedQuery):
        if filename:
            if "newname" in parsedQuery:
                newname = parsedQuery["newname"][0]
                debug("renaming " + filepath(username, filename) + " to " + newname)

                result = files.rename(username, filename, newname)
                if result[0]:
                    return Responder.OK()
                else:
                    return Responder.error("could not rename " + filepath(username, filename) + " to " + newname + " (" + result[1] + ")")
            else:
                return Responder.error("no new filename")
        else:
            return Responder.error("no old filename")

    def copy(username, filename, data, parsedQuery):
        if filename:
            if "tofile" in parsedQuery:
                tofile = parsedQuery["tofile"][0]
                debug("copying " + filepath(username, filename) + " to " + tofile)

                result = files.copyFile(username, filename, tofile)
                if result[0]:
                    return Responder.OK()
                else:
                    return Responder.error("could not copy " + filepath(username, filename) + " to " + tofile + " (" + result[1] + ")")
            else:
                return Responder.error("no destination filename")
        else:
            return Responder.error("no source filename")

    def write(username, filename, data, parsedQuery):
        if filename:
            if data:
                debug("writing " + data + " to " + filepath(username, filename))

                f = files.file(username, filename)   

                f.write(bytes(data, "utf-8"))     # bytearray in Python2  

                return Responder.OK()
            else:
                return Responder.error("no data to be written")
        else:
            return Responder.error("no filename")

    def server(username, filename, data, parsedQuery): # Ignore filename
        if data:
            if data == "sfs_version":
                return Responder.writeResult(VERSION)
            elif data == "python_version":
                return Responder.writeResult(sys.version)
            else:
                return Responder.error("invalid server information request")
        else:
            return Responder.error("no server information requested")

    CALL_TABLE = {"readall": readall, "append": append, "read": read, "closeall": closeAll, "atend": atEnd, "close": close, "truncate": truncate,
                  "setposition": setPosition, "getposition": position, "exists": exists, "delete": delete, "rename": rename, "copy": copy, "write": write, "server": server}

    def handle(command, username, filename, data, parsedQuery):
        if command in Responder.CALL_TABLE:
            return Responder.CALL_TABLE[command](username, filename, data, parsedQuery)
        else:
            return Responder.writeResult("invalid command " + command)


class CORSHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):

        # debug(str(self.client_address))

        path = self.path
        debug("path=" + path)
        # path loResponder.OKs like /<command>?<querystring>, e.g., /readall?file=<filename> etc

        parsedPath = urllib.parse.urlparse(path)
        # parsedPath loResponder.OKs like this: ParseResult(scheme='', netloc='', path='/readall', params='', query='file=test.txt', fragment='')
        command = parsedPath.path.lower()
        debug("command=" + command)

        query = parsedPath.query
        debug("query=" + query)

        parsedQuery = urllib.parse.parse_qs(query)
        if "user" in parsedQuery:
            username = parsedQuery["user"][0]
        else:
            username = ""

        if "file" in parsedQuery:
            filename = parsedQuery["file"][0]
        else:
            filename = None

        if "data" in parsedQuery:
            data = parsedQuery["data"][0]
        else:
            data = None
        # filename and data are very common, so we deal with them immediately

        temporaryFile = Responder.handle(command[1:], username, filename, data, parsedQuery)
        # command is like /append, /server

        debug("temporary file: " + temporaryFile)

        f = open(temporaryFile, "rb")
        ctype = self.guess_type(temporaryFile)

        self.send_response(200)
        self.send_header("Content-type", ctype)
        fs = os.fstat(f.fileno())
        self.send_header("Content-Length", str(fs[6]))
        self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        return f

 
if __name__ == "__main__":

    Handler = CORSHTTPRequestHandler

    files = FileCache()
    
    if not os.path.exists(snapFilesPath("")):
        os.makedirs(snapFilesPath(""))
    # Make sure that ~/Documents/SnapFiles exists, creating it, and
    #   any intermediate directories, if it doesn't            

    httpd = socketserver.TCPServer(("", PORT), Handler)

    debug("Serving at port " + str(PORT))
    debug("Go ahead and launch Snap!")
    debug("Home is " + HOMEPath())
    debug("Temporary file is " + TEMPORARY_FILE_NAME)

    httpd.serve_forever()


