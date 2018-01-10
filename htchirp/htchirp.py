import re
import os
import stat
import socket

# RFC 2396 quoting method (Chirp protocol 2 compliant, but not used)
#try: # Python 3
#    from urllib.parse import quote
#except ImportError: # Python 2
#    from urllib import quote

# In the HTCondor implementation, this quoting method is used
def quote(chirp_string):
    """
    Prepares a string to be used in a Chirp simple command

    :param chirp_string: the string to prepare
    :returns: escaped string

    """

    escape_chars = ["\\", " ", "\n", "\t", "\r"]
    escape_re = "(" + "|".join([re.escape(x) for x in escape_chars]) + ")"
    escape = re.compile(escape_re)

    # prepend escaped characters with \\
    replace = lambda matchobj: "\\" + matchobj.group(0)
    return escape.sub(replace, chirp_string)


class HTChirp:
    """Chirp client for HTCondor

    Provides Chirp commands compatible with the HTCondor Chirp implementation
    with the exception of the following commands:

    * read
    * write

    Some commands have been renamed (and aliased) to follow the Chirp protocol:
    fetch -> getfile
    put -> putfile
    remove -> unlink

    If the host and port of a Chirp server are not specified, you are assumed
    to be running in a HTCondor "+WantIOProxy = true" job and that
    $_CONDOR_SCRATCH_DIR/.chirp.config contains the host, port, and cookie for
    connecting to the embedded chirp proxy.

    """

    ## static reference variables

    CHIRP_PROTOCOL_VERSION = 2
    CHIRP_LINE_MAX = 1024
    CHIRP_AUTH_METHODS = ["cookie", "hostname", "unix", "kerberos", "globus"]
    DEFAULT_MODE = (
        (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) |
        (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) |
        (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) )


    ## initialize

    def __init__(self,
                     host = None,
                     port = None,
                     auth = ["cookie"],
                     cookie = None,
                     timeout = 10):
        """Chirp client initialization

        :param host: the hostname or ip of the Chirp server
        :param port: the port of the Chirp server
        :param auth: a list of authentication methods to try
        :param cookie: the cookie string, if trying cookie authentication
        :param timeout: socket timeout, in seconds

        """

        # initialize storage variables
        self.fds = {} # open file descriptors

        chirp_config = ".chirp.config"
        try:
            chirp_config = os.path.join(
                os.environ["_CONDOR_SCRATCH_DIR"], chirp_config)
        except KeyError:
            pass

        if (host and port): # don't read chirp_config if host and port are set
            pass
        elif (("cookie" in auth)
                  and (not cookie)
                  and os.path.isfile(chirp_config)): # read chirp_config
            try:
                with open(chirp_config, "r") as f:
                    (host, port, cookie) = f.read().rstrip().split()
            except Exception:
                print("Error reading {0}".format(chirp_config))
                raise
        else:
            raise ValueError((".chirp.config must be present "
                                  "or you must provide a host and port"))

        # store connection parameters
        self.__host = host
        self.__port = int(port)
        self.__cookie = cookie
        self.__timeout = timeout

        # connect and store authentication method
        self.__authentication = None
        for auth_method in auth:
            try:
                self.__connect(auth_method)
            except self.NotAuthenticated:
                self.__disconnect()
            except NotImplementedError:
                self.__disconnect()
                raise
            else:
                self.__disconnect()
                self.__authentication = auth_method
                break
        if self.__authentication == None:
            raise self.NotAuthenticated(
                "Could not authenticate with methods {0}".format(auth))


    ## special methods

    def __del__(self):
        """Disconnect from the Chirp server when this object goes away"""
        self.__disconnect()

    def __repr__(self):
        """Print a representation of this object"""
        return "{0}({1}, {2}) using {3} authentication".format(
            self.__class__.__name__,
            self.__host,
            self.__port,
            self.__authentication)


    ## internal methods

    def __connect(self, auth_method = None):
        """Connect to and authenticate with the Chirp server

        :param auth_method: If set, try the specific authentication method

        """

        if not auth_method:
            auth_method = self.__authentication

        # close the socket if it is open and exists
        try:
            self.__socket.getsockname()
        except (NameError, AttributeError):
            pass # socket object does not exist
        except socket.error:
            pass # socket exists but is closed
        else:
            # socket exists and is connected
            self.__disconnect()

        # create the socket
        self.__socket = socket.socket()
        self.__socket.settimeout(self.__timeout)

        # connect and authenticate
        self.__socket.connect((self.__host, self.__port))
        self.__authenticate(auth_method)

        # reset open file descriptors
        self.fds = {}

    def __authenticate(self, method):
        """Test authentication method

        :param method: The authentication method to attempt

        """

        if method == "cookie":
            response = self.__simple_command("cookie {0}\n".format(
                self.__cookie))
            if not (str(response) == "0"):
                raise self.NotAuthenticated(
                    "Could not authenticate using {0}".format(method))
        elif method in self.__class__.CHIRP_AUTH_METHODS:
            raise NotImplementedError(
                "Auth method '{0}' not implemented in this client".format(
                    method))
        else:
            raise ValueError("Unknown authentication method '{0}'".format(
                method))

    def __disconnect(self):
        """Close connection with the Chirp server"""

        try:
            self.__socket.close()
        except socket.error:
            pass
        except (NameError, AttributeError):
            pass

        # reset open file descriptors
        self.fds = {}

    def __simple_command(self, cmd):
        """Send a command to the Chirp server

        :param cmd: The command to be sent
        :returns: The response from the Chirp server
        :raises InvalidRequest: If the command is invalid
        :raises RuntimeError: If the connection is broken

        """

        # check the command
        if cmd[-1] != "\n":
            raise self.InvalidRequest("The form of the request is invalid.")

        # send the command
        bytes_sent = 0
        while bytes_sent < len(cmd):
            sent = self.__socket.send(cmd[bytes_sent:])
            if sent == 0:
                raise RuntimeError("Connection to the Chirp server is broken.")
            bytes_sent = bytes_sent + sent

        return self.__simple_response()

    def __simple_response(self):
        """Get the response from the Chirp server after running a command

        :returns: The response from the Chirp server
        :raises EnvironmentError: if response is too large

        """

        # build up the response one byte at a time
        response = b""
        chunk = b""
        while chunk != "\n": # response terminated with \n
            chunk = self.__socket.recv(1)
            response += chunk
            # make sure response doesn't get too large
            if len(response) > self.__class__.CHIRP_LINE_MAX:
                raise EnvironmentError("The server responded with too much data.")
        response = response.rstrip()

        # check the response code if an int is returned
        try:
            int(response)
        except ValueError:
            pass
        else:
            self.__check_response(int(response))

        return response

    def __check_response(self, response):
        """Check the response from the Chirp server for validity

        :raises ChirpError: Many different subclasses of ChirpError

        """

        chirp_errors = {
            -1: self.NotAuthenticated("The client has not authenticated its identity."),
            -2: self.NotAuthorized("The client is not authorized to perform that action."),
            -3: self.DoesntExist("There is no object by that name."),
            -4: self.AlreadyExists("There is already an object by that name."),
            -5: self.TooBig("That request is too big to execute."),
            -6: self.NoSpace("There is not enough space to store that."),
            -7: self.NoMemory("The server is out of memory."),
            -8: self.InvalidRequest("The form of the request is invalid."),
            -9: self.TooManyOpen("There are too many resources in use."),
            -10: self.Busy("That object is in use by someone else."),
            -11: self.TryAgain("A temporary condition prevented the request."),
            -12: self.BadFD("The file descriptor requested is invalid."),
            -13: self.IsDir("A file-only operation was attempted on a directory."),
            -14: self.NotDir("A directory operation was attempted on a file."),
            -15: self.NotEmpty("A directory cannot be removed because it is not empty."),
            -16: self.CrossDeviceLink("A hard link was attempted across devices."),
            -17: self.Offline("The requested resource is temporarily not available."),
            -127: self.UnknownError("An unknown error (-127) occured."),
        }

        if response in chirp_errors:
            raise chirp_errors[response]
        elif response < 0:
            raise self.UnknownError("An unknown error ({0}) occured.".format(
                response))

    def __get_fixed_data(self, length, output_file = None):
        """Get a fixed amount of data from the Chirp server

        :param length: The amount of data (in bytes) to receive
        :param output_file: Output file to store received data (optional)
        :returns: Received data, unless output_file is set, then returns number
            of bytes received.

        """

        if output_file: # stream data to a file
            bytes_recv = 0
            chunk = b""
            with open(output_file, "wb") as fd:
                while bytes_recv < length:
                    chunk = self.__socket.recv(self.__class__.CHIRP_LINE_MAX)
                    fd.write(chunk)
                    bytes_recv += len(chunk)
            return bytes_recv

        else: # return data to method call
            data = b""
            chunk = b""
            while len(data) < length:
                chunk = self.__socket.recv(self.__class__.CHIRP_LINE_MAX)
                data += chunk
            return data

    def __get_line_data(self):
        """Get one line of data from the Chirp server

        Most chirp commands return the length of data that will be returned, in
        which case the __get_fixed_data method should be used. This is for the
        few commands (stat, lstat) that do not return a fixed length.

        :returns: A line of data received from the Chirp server

        """

        data = b""
        while True:
            data += self.__socket.recv(self.__class__.CHIRP_LINE_MAX)
            if (data[-1] == "\n"):
                break
        return data

    def __open(self, name, flags, mode = None):
        """Open a file on the Chirp server

        :param name: Path to file
        :param flags: File open modes (one or more of 'rwatcx')
        :param mode: Permission mode to set, in decimal (e.g. 511 or 0777). If
            not specified, defaults to 0777
        :returns: File descriptor

        """

        # set the default permission
        if mode == None:
            mode = self.__class__.DEFAULT_MODE

        # check flags
        valid_flags = set('rwatcx')
        flags = set(flags)
        if not flags.issubset(valid_flags):
            raise ValueError("Flags must be one or more of 'rwatcx'")

        # get file descriptor
        fd = int(self.__simple_command("open {0} {1} {2}\n".format(
            quote(name),
            ''.join(flags),
            int(mode))))

        # store file info
        file_info = (quote(name), ''.join(flags), int(mode))
        self.fds[fd] = file_info

        return fd

    def __close(self, fd):
        """Close a file on the Chirp server

        :param fd: File descriptor

        """
        
        self.__simple_command("close {0}\n".format(int(fd)))

    def __read(self,
                   fd, length,
                   offset = None,
                   stride_length = None, stride_skip = None):
        """Read from a file on the Chirp server

        :param fd: File descriptor
        :param length: Number of bytes to read
        :param offset: Skip this many bytes when reading
        :param stride_length: Read this many bytes every stride_skip bytes
        :param stride_skip: Skip this many bytes between reads
        :returns: Data read from file

        """

        if offset == None and (stride_length, stride_skip) != (None, None):
            offset = 0 # assume offset is 0 if stride given but not offset

        if (offset, stride_length, stride_skip) == (None, None, None):
            # read
            rb = self.__simple_command("read {0} {1}\n".format(
                int(fd),
                int(length)))
        elif (offset != None) and (stride_length, stride_skip) == (None, None):
            # pread
            rb = self.__simple_command("pread {0} {1} {2}\n".format(
                int(fd),
                int(length),
                int(offset)))
        elif (stride_length, stride_skip) != (None, None):
            # sread
            rb = self.__simple_command("sread {0} {1} {2} {3} {4}\n".format(
                int(fd),
                int(length),
                int(offset),
                int(stride_length),
                int(stride_skip)))
        else:
            raise self.InvalidRequest(
                "Both stride_length and stride_skip must be specified")

        return self.__get_fixed_data(rb)

    def __write(self, data,
                    fd, length,
                    offset = None,
                    stride_length = None, stride_skip = None):
        """Write to a file on the Chirp server

        :param data: Data to write
        :param fd: File descriptor
        :param length: Number of bytes to write
        :param offset: Skip this many bytes when writing
        :param stride_length: Write this many bytes every stride_skip bytes
        :param stride_skip: Skip this many bytes between writes
        :returns: Number of bytes written

        """

        if offset == None and (stride_length, stride_skip) != (None, None):
            offset = 0 # assume offset is 0 if stride given but not offset

        if (offset, stride_length, stride_skip) == (None, None, None):
            # write
            wb = self.__simple_command("write {0} {1}\n".format(
                int(fd),
                int(length)))
        elif (offset != None) and (stride_length, stride_skip) == (None, None):
            # pwrite
            wb = self.__simple_command("pwrite {0} {1} {2}\n".format(
                int(fd),
                int(length),
                int(offset)))
        elif (stride_length, stride_skip) != (None, None):
            # swrite
            wb = self.__simple_command("swrite {0} {1} {2} {3} {4}\n".format(
                int(fd),
                int(length),
                int(offset),
                int(stride_length),
                int(stride_skip)))
        else:
            raise self.InvalidRequest(
                "Both stride_length and stride_skip must be specified")

        wfd = self.__socket.makefile("wb") # open socket as a file object
        wfd.write(data[:int(wb)]) # write up to wb bytes
        wfd.close() # close socket file object

        return int(wb)

    def __fsync(self, fd):
        """Flush unwritten data to disk

        :param fd: File descriptor

        """
        
        self.__simple_command("fsync {0}\n".format(int(fd)))

    def __lseek(self, fd, offset, whence):
        """Move the position of a pointer in an open file

        :param fd: File descriptor
        :param offset: Number of bytes to move pointer
        :param whence: Where to base the offset from
        :returns: Position of pointer

        """

        pos = self.__simple_command("lseek {0} {1} {2}\n".format(
            int(fd),
            int(offset),
            int(whence)))
        return int(pos)

    ## public methods

    # HTCondor-specific methods

    def get_job_attr(self, job_attribute):
        """Get the value of a job ClassAd attribute.

        :param job_attribute: The job attribute to query
        :returns: The value of the job attribute as a string

        """

        self.__connect()
        length = int(self.__simple_command("get_job_attr {0}\n".format(
            quote(job_attribute))))
        result = self.__get_fixed_data(length)
        self.__disconnect()

        return str(result)

    def get_job_attr_delayed(self, job_attribute):
        """Get the value of a job ClassAd attribute from the local Starter.

        This may differ from the value in the Schedd.

        :param job_attribute: The job attribute to query
        :returns: The value of the job attribute as a string

        """

        self.__connect()
        length = int(self.__simple_command("get_job_attr_delayed {0}\n".format(
            quote(job_attribute))))
        result = self.__get_fixed_data(length)
        self.__disconnect()

        return str(result)

    def set_job_attr(self, job_attribute, attribute_value):
        """Set the value of a job ClassAd attribute.

        :param job_attribute: The job attribute to set
        :param attribute_value: The job attribute's new value

        """

        self.__connect()
        self.__simple_command("set_job_attr {0} {1}\n".format(
            quote(job_attribute),
            quote(attribute_value)))
        self.__disconnect()

    def set_job_attr_delayed(self, job_attribute, attribute_value):
        """Set the value of a job ClassAd attribute.

        This variant of set_job_attr will not push the update immediately, but
        rather as a non-durable update during the next communication between
        starter and shadow.

        :param job_attribute: The job attribute to set
        :param attribute_value: The job attribute's new value

        """
        self.__connect()
        self.__simple_command("set_job_attr_delayed {0} {1}\n".format(
            quote(job_attribute),
            quote(attribute_value)))
        self.__disconnect()

    def ulog(self, text):
        """Log a generic string to the job log.

        :param text: String to log

        """

        self.__connect()
        self.__simple_command("ulog {0}\n".format(
            quote(text)))
        self.__disconnect()

    def phase(self, phasestring):
        """Tell HTCondor that the job is changing phases.

        :param phasestring: New phase

        """

        self.__connect()
        self.__simple_command("phase {0}\n".format(
            quote(phasestring)))
        self.__disconnect()

    # Chirp protocol standard methods

    def rename(self, old_path, new_path):
        """Rename (move) a file on the remote machine.

        :param old_path: Path to file to be renamed
        :param new_path: Path to new file name

        """

        self.__connect()
        self.__simple_command("rename {0} {1}\n".format(
            quote(old_path),
            quote(new_path)))
        self.__disconnect()

    def unlink(self, remote_file):
        """Delete a file on the remote machine.

        :param remote_file: Path to file

        """

        self.__connect()
        self.__simple_command("unlink {0}\n".format(
            quote(remote_file)))
        self.__disconnect()

    # alias remove -> unlink
    remove = unlink

    def rmdir(self, remote_path, recursive = False):
        """Delete a directory on the remote machine.

        The directory must be empty unless recursive is set to True.

        :param remote_path: Path to directory
        :param recursive: If set to True, recursively delete remote_path

        """

        if recursive == True:
            self.__rmall(remote_path)
        else:
            self.__connect()
            self.__simple_command("rmdir {0}\n".format(
                quote(remote_path)))
            self.__disconnect()

    def rmall(self, remote_path):
        """Recursively delete an entire directory on the remote machine.

        :param remote_path: Path to directory

        """

        self.__connect()
        self.__simple_command("rmall {0}\n".format(
            quote(remote_path)))
        self._disconnect()

    def mkdir(self, remote_path, mode = None):
        """Create a new directory on the remote machine.

        :param remote_path: Path to new directory
        :param mode: Permission mode to set, in decimal (e.g. 511 or 0777). If
            not specified, defaults to 0777

        """

        # set the default permission
        if mode == None:
            mode = self.__class__.DEFAULT_MODE

        self.__connect()
        self.__simple_command("mkdir {0} {1}\n".format(
            quote(remote_path),
            int(mode)))
        self.__disconnect()

    def getfile(self, remote_file, local_file):
        """Retrieve an entire file efficiently from the remote machine.

        :param remote_file: Path to file to be sent from remote machine
        :param local_file: Path to file to be written to on local machine
        :returns: Bytes written

        """

        self.__connect()
        length = int(self.__simple_command("getfile {0}\n".format(
            quote(remote_file))))
        bytes_recv = self.__get_fixed_data(length, local_file)
        self.__disconnect()

        return bytes_recv

    # alias fetch -> getfile
    fetch = getfile

    def putfile(self, local_file, remote_file, mode = None):
        """Store an entire file efficiently to the remote machine.

        :param local_file: Path to file to be sent from local machine
        :param remote_file: Path to file to be written to on remote machine
        :param mode: Permission mode to set, in decimal (e.g. 511 or 0777). If
            not specified, defaults to 0777
        :returns: Size of written file

        """

        # set the default permission
        if mode == None:
            mode = self.__class__.DEFAULT_MODE

        # get file size
        length = os.stat(local_file).st_size
        bytes_sent = 0

        # send the file
        self.__connect()
        self.__simple_command("putfile {0} {1} {2}\n".format(
            quote(remote_file),
            int(mode),
            int(length)))
        wfd = self.__socket.makefile("wb") # open socket as a file object
        with open(local_file, "rb") as rfd:
            data = rfd.read(self.__class__.CHIRP_LINE_MAX)
            while data: # write to socket CHIRP_LINE_MAX bytes at a time
                wfd.write(data)
                bytes_sent += len(data)
                data = rfd.read(self.__class__.CHIRP_LINE_MAX)
        wfd.close()
        self.__disconnect()

        return bytes_sent

    # alias put -> putfile
    put = putfile

    def getlongdir(self, remote_path):
        """List a directory and all its file metadata on the remote machine.

        :param remote_path: Path to directory
        :returns: A dict of file metadata

        """

        names = ["device", "inode", "mode", "nlink", "uid", "gid", "rdevice"
                     "size", "blksize", "blocks", "atime", "mtime", "ctime"]

        self.__connect()
        length = int(self.__simple_command("getlongdir {0}\n".format(
            quote(remote_path))))
        result = self.__get_fixed_data(length)
        self.__disconnect()

        results = result.rstrip().split("\n")
        files = results[::2]
        stat_dicts = [dict(zip(names, [int(x) for x in s.split()]))
                          for s in results[1::2]]
        return dict(zip(files, stat_dicts))

    def getdir(self, remote_path, stat_dict = False):
        """List a directory on the remote machine.

        :param remote_path: Path to directory
        :param stat_dict: If set to True, return a dict of file metadata
        :returns: List of files, unless stat_dict is True

        """

        if stat_dict == True:
            return getlongdir(remote_path)
        else:
            self.__connect()
            length = int(self.__simple_command("getdir {0}\n".format(
                quote(remote_path))))
            result = self.__get_fixed_data(length)
            self.__disconnect()

            files = result.rstrip().split("\n")
            return files

    def whoami(self):
        """Get the user's current identity with respect to this server.

        :returns: The user's identity

        """

        self.__connect()
        length = int(self.__simple_command("whoami {0}\n".format(
            self.__class__.CHIRP_LINE_MAX)))
        result = self.__get_fixed_data(length)
        self.__disconnect()

        return str(result)

    def whoareyou(self, remote_host):
        """Get the server's identity with respect to the remote host.

        :param remote_host: Remote host
        :returns: The server's identity

        """

        self.__connect()
        length = int(self.__simple_command("whoareyou {0} {1}\n".format(
            quote(remote_host),
            self.__class__.CHIRP_LINE_MAX)))
        result = self.__get_fixed_data(length)
        self.__disconnect()

        return str(result)

    def link(self, old_path, new_path, symbolic = False):
        """Create a link on the remote machine.

        :param old_path: File path to link from on the remote machine
        :param new_path: File path to link to on the remote machine
        :param symbolic: If set to True, use a symbolic link

        """

        if symbolic:
            self.__symlink(old_path, new_path)
        else:
            self.__connect()
            self.__simple_command("link {0} {1}\n".format(
                quote(old_path),
                quote(new_path)))
            self.__disconnect()

    def symlink(self, old_path, new_path):
        """Create a symbolic link on the remote machine.

        :param old_path: File path to symlink from on the remote machine
        :param new_path: File path to symlink to on the remote machine

        """

        self.__connect()
        self.__simple_command("symlink {0} {1}\n".format(
            quote(old_path),
            quote(new_path)))
        self.__disconnect()

    def readlink(self, remote_path):
        """Read the contents of a symbolic link.

        :param remote_path: File path on the remote machine
        :returns: Contents of the link

        """

        self.__connect()
        length = self.__simple_command("readlink {0}\n".format(
            quote(remote_path)))
        result = self.__get_fixed_data(length)
        self.__disconnect()
        
        return result

    def stat(self, remote_path):
        """Get metadata for file on the remote machine.

        If remote_path is a symbolic link, examine its target.

        :param remote_path: Path to file
        :returns: Dict of file metadata

        """

        names = ["device", "inode", "mode", "nlink", "uid", "gid", "rdevice",
                     "size", "blksize", "blocks", "atime", "mtime", "ctime"]

        self.__connect()
        response = self.__simple_command("stat {0}\n".format(
            quote(remote_path)))
        result = str(self.__get_line_data()).rstrip()
        while len(result.split()) < len(names):
            result += (" " + str(self.__get_line_data()).rstrip())
        self.__disconnect()

        results = [int(x) for x in result.split()]
        return dict(zip(names, results))

    def lstat(self, remote_path):
        """Get metadata for file on the remote machine.

        If remote path is a symbolic link, examine the link.

        :param remote_path: Path to file
        :returns: Dict of file metadata

        """

        names = ["device", "inode", "mode", "nlink", "uid", "gid", "rdevice",
                     "size", "blksize", "blocks", "atime", "mtime", "ctime"]

        self.__connect()
        response = self.__simple_command("lstat {0}\n".format(
            quote(remote_path)))
        result = str(self.__get_line_data()).rstrip()
        while len(result.split()) < len(names):
            result += (" " + str(self.__get_line_data()).rstrip())
        self.__disconnect()

        results = [int(x) for x in result.split()]
        stats = dict(zip(names, results))
        return stats

    def statfs(self, remote_path):
        """Get metadata for a file system on the remote machine.

        :param remote_path: Path to examine
        :returns: Dict of filesystem metadata

        """

        names = ["type", "bsize", "blocks", "bfree", "bavail", "files", "free"]
        names = ["f_" + x for x in names]

        self.__connect()
        response = self.__simple_command("statfs {0}\n".format(
            quote(remote_path)))
        result = str(self.__get_line_data()).rstrip()
        while len(result.split()) < len(names):
            result += (" " + str(self.__get_line_data()).rstrip())
        self.__disconnect()

        results = [int(x) for x in result.split()]
        stats = dict(zip(names, results))
        return stats

    def access(self, remote_path, mode_str):
        """Check access permissions.

        :param remote_path: Path to examine
        :param mode_str: Mode to check, a string of one or more of (frwx)
        :raises NotAuthorized: If any access mode is not authorized

        """

        modes = {
            "f": 0,
            "r": stat.S_IROTH,
            "w": stat.S_IWOTH,
            "x": stat.S_IXOTH
            }

        mode = 0
        for m in mode_str:
            if m not in modes:
                raise ValueError("mode '{0}' not in (fxwr)".format(m))
            mode = mode | modes[m]

        self.__connect()
        self.__simple_command("access {0} {1}\n".format(
            quote(remote_path),
            mode_oct))
        self.__disconnect()

    def chmod(self, remote_path, mode):
        """Change permission mode of a path on the remote machine.

        :param remote_path: Path
        :param mode: Permission mode to set, in decimal (e.g. 511 or 0777)

        """

        self.__connect()
        self.__simple_command("chmod {0} {1}\n".format(
            quote(remote_path),
            int(mode)))
        self.__disconnect()

    def chown(self, remote_path, uid, gid):
        """Change the UID and/or GID of a path on the remote machine.

        If remote_path is a symbolic link, change its target.

        :param remote_path: Path
        :param uid: UID
        :param gid: GID

        """

        self.__connect()
        self.__simple_command("chown {0} {1} {2}\n".format(
            quote(remote_path),
            int(uid),
            int(gid)))
        self.__disconnect()

    def lchown(self, remote_path, uid, gid):
        """Changes the ownership of a file or directory.

        If the path is a symbolic link, change the link.

        :param remote_path: Path
        :param uid: UID
        :param gid: GID

        """

        self.__connect()
        self.__simple_command("lchown {0} {1} {2}\n".format(
            quote(remote_path),
            int(uid),
            int(gid)))
        self.__disconnect()

    def truncate(self, remote_path, length):
        """Truncates a file on the remote machine to a given number of bytes.

        :param remote_path: Path to file
        :param length: Truncated length

        """

        self.__connect()
        self.__simple_command("truncate {0} {1}\n".format(
            quote(remote_path),
            int(length)))
        self.__disconnect()

    def utime(self, remote_path, actime, mtime):
        """Change the access and modification times of a file
        on the remote machine.

        :param remote_path: Path to file
        :param actime: Access time, in seconds (Unix epoch)
        :param mtime: Modification time, in seconds (Unix epoch)

        """

        self.__connect()
        self.__simple_command("utime {0} {1} {2}\n".format(
            quote(remote_path),
            int(actime),
            int(mtime)))
        self.__disconnect()

    ## Chirp commands that are not implemented in HTCondor

    # def getacl(self, remote_path):
    #     """Get an access control list.
    #
    #     :param remote_path: Path to examine
    #     :returns: List with each entry of the access control list
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("getacl {0}\n".format(
    #         quote(remote_path)))
    #     acl = []
    #     while True:
    #         entry = self.__get_line_data().rstrip()
    #         if entry == "":
    #             break
    #         acl.append(entry)
    #     self.__disconnect()
    #     return acl

    # def setacl(self, remote_path, subject, rights):
    #     """Modify an access control list on an object on the remote machine.
    #
    #     :param remote_path: Path to modify
    #     :param subject: Subject
    #     :param rights: Rights ("-" for no rights)
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("setacl {0} {1} {2}\n".format(
    #         quote(remote_path),
    #         quote(subject),
    #         quote(rights)))
    #     self.__disconnect()

    # def md5(self, remote_path):
    #     """Checksum a file on the remote machine using MD5.
    #
    #     :param remote_path: Path to file
    #     :returns: A string containing the md5 hash
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("md5 {0}\n".format(
    #         quote(remote_path)))
    #     self.__disconnect()

    # def thirdput(self, remote_path, third_host, third_path):
    #     """Direct the remote machine to transfer the path to another ("third")
    #     remote host and path.
    #
    #     If the indicated path is a directory, it will be transferred recursively,
    #     preserving metadata such as access control lists.
    #
    #     :param remote_path: Path to transfer from the remote machine
    #     :param third_host: Host to transfer to
    #     :param third_path: Path to transfer to on the third machine
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("thirdput {0} {1} {2}\n".format(
    #         quote(remote_path),
    #         quote(third_host),
    #         quote(third_path)))
    #     self.__disconnect()

    # def mkalloc(self, remote_path, size, mode):
    #     """Create a new space allocated on the remote machine at the given path.
    #
    #     :param remote_path: Path
    #     :param size: Size of allocation in bytes
    #     :param mode: Permission mode to set, in decimal (e.g. 511 or 0777)
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("mkalloc {0} {1} {2}\n".format(
    #         quote(remote_path),
    #         int(size),
    #         int(mode)))
    #     self.__disconnect()

    # def lsalloc(self, remote_path):
    #     """List the space allocation state on a directory on the remote machine.
    #
    #     :param remote_path: Path
    #     :returns: Tuple containing the path, size, and mode of the allocation
    #
    #     """
    #
    #     self.__connect()
    #     self.__simple_command("lsalloc {0}\n".format(
    #         quote(remote_path)))
    #     result = self.__get_line_data()
    #     self.__disconnect()
    #     return tuple(result.split())


    ## custom exceptions

    class ChirpError(Exception):
        """Base class for all chirp errors."""
        pass

    class NotAuthenticated(ChirpError):
        pass

    class NotAuthorized(ChirpError):
        pass

    class DoesntExist(ChirpError):
        pass

    class AlreadyExists(ChirpError):
        pass

    class TooBig(ChirpError):
        pass

    class NoSpace(ChirpError):
        pass

    class NoMemory(ChirpError):
        pass

    class InvalidRequest(ChirpError):
        pass

    class TooManyOpen(ChirpError):
        pass

    class Busy(ChirpError):
        pass

    class TryAgain(ChirpError):
        pass

    class BadFD(ChirpError):
        pass

    class IsDir(ChirpError):
        pass

    class NotDir(ChirpError):
        pass

    class NotEmpty(ChirpError):
        pass

    class CrossDeviceLink(ChirpError):
        pass

    class Offline(ChirpError):
        pass

    class UnknownError(ChirpError):
        pass