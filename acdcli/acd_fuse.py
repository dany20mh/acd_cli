"""fusepy filesystem module"""

import configparser
import errno
import json
import logging
import os
import stat
import sys
import tempfile

from collections import deque, defaultdict
from multiprocessing import Process
from threading import Thread, Lock
from time import time

import ctypes.util
import binascii

from acdcli.cache.db import CacheConsts

ctypes.util.__find_library = ctypes.util.find_library

def find_library(*args):
    if 'fuse' in args[0]:
        libfuse_path = os.environ.get('LIBFUSE_PATH')
        if libfuse_path:
            return libfuse_path

    return ctypes.util.__find_library(*args)

ctypes.util.find_library = find_library

from fuse import FUSE, FuseOSError as FuseError, Operations
from acdcli.api.common import RequestError
from acdcli.utils.conf import get_conf
from acdcli.utils.time import *

logger = logging.getLogger(__name__)

try:
    errno.ECOMM
except:
    errno.ECOMM = errno.ECONNABORTED
try:
    errno.EREMOTEIO
except:
    errno.EREMOTEIO = errno.EIO

_SETTINGS_FILENAME = 'fuse.ini'
_XATTR_PROPERTY_NAME = 'xattrs'
_XATTR_MTIME_OVERRIDE_NAME = 'fuse.mtime'

_def_conf = configparser.ConfigParser()
_def_conf['read'] = dict(open_chunk_limit=10, timeout=5)
_def_conf['write'] = dict(buffer_size=int(1e9), timeout=30)


class FuseOSError(FuseError):
    def __init__(self, err_no: int):
        # logger.debug('FUSE error %i, %s.' % (err_no, errno.errorcode[err_no]))
        super().__init__(err_no)

    CODE = RequestError.CODE
    codes = RequestError.codes
    code_mapping = {CODE.CONN_EXCEPTION: FuseError(errno.ECOMM),
                    codes.CONFLICT: FuseError(errno.EEXIST),
                    codes.REQUESTED_RANGE_NOT_SATISFIABLE: FuseError(errno.EFAULT),
                    codes.REQUEST_TIMEOUT: FuseError(errno.ETIMEDOUT),
                    codes.GATEWAY_TIMEOUT: FuseError(errno.ETIMEDOUT)
                    }

    @staticmethod
    def convert(e: RequestError):
        """:raises: FuseOSError"""

        try:
            caller = sys._getframe().f_back.f_code.co_name + ': '
        except AttributeError:
            caller = ''
        logger.error(caller + e.__str__())

        try:
            exc = FuseOSError.code_mapping[e.status_code]
        except AttributeError:
            exc = FuseOSError(errno.EREMOTEIO)
        raise exc


class ReadProxy(object):
    """Dict of stream chunks for consecutive read access of files."""

    def __init__(self, acd_client, open_chunk_limit, timeout):
        self.acd_client = acd_client
        self.lock = Lock()
        self.files = defaultdict(lambda: ReadProxy.ReadFile(open_chunk_limit, timeout))

    class StreamChunk(object):
        """StreamChunk represents a file node chunk as a streamed ranged HTTP response
        which may or may not be partially read."""

        __slots__ = ('offset', 'r', 'end')

        def __init__(self, acd_client, id_, offset, length, **kwargs):
            self.offset = offset
            """the first byte position (fpos) available in the chunk"""

            self.r = acd_client.response_chunk(id_, offset, length, **kwargs)
            """:type: requests.Response"""

            self.end = offset + int(self.r.headers['content-length']) - 1
            """the last byte position (fpos) contained in the chunk"""

        def has_byte_range(self, offset, length) -> bool:
            """Tests whether chunk begins at **offset** and has at least **length** bytes remaining.
            """
            logger.debug('s: %d-%d; r: %d-%d'
                         % (self.offset, self.end, offset, offset + length - 1))
            if offset == self.offset and offset + length - 1 <= self.end:
                return True
            return False

        def get(self, length) -> bytes:
            """Gets *length* bytes beginning at current offset.

            :param length: the number of bytes to get
            :raises: Exception if less than *length* bytes were received \
             but end of chunk was not reached"""

            b = next(self.r.iter_content(length))
            self.offset += len(b)

            if len(b) < length and self.offset <= self.end:
                logger.warning('Chunk ended unexpectedly.')
                raise Exception
            return b

        def close(self):
            """Closes connection on the stream."""
            self.r.close()

    class ReadFile(object):
        """Represents a file opened for reading.
        Encapsulates at most :attr:`MAX_CHUNKS_PER_FILE` open chunks."""

        __slots__ = ('chunks', 'access', 'lock', 'timeout')

        def __init__(self, open_chunk_limit, timeout):
            self.chunks = deque(maxlen=open_chunk_limit)
            self.access = time()
            self.lock = Lock()
            self.timeout = timeout

        def get(self, acd_client, id_, offset, length, total) -> bytes:
            """Gets a byte range from existing StreamChunks"""

            with self.lock:
                i = len(self.chunks) - 1
                while i >= 0:
                    c = self.chunks[i]
                    if c.has_byte_range(offset, length):
                        try:
                            bytes_ = c.get(length)
                        except:
                            self.chunks.remove(c)
                        else:
                            return bytes_
                    i -= 1

            try:
                with self.lock:
                    chunk = ReadProxy.StreamChunk(acd_client, id_, offset,
                                                  acd_client._conf.getint('transfer',
                                                                          'dl_chunk_size'),
                                                  timeout=self.timeout)
                    if len(self.chunks) == self.chunks.maxlen:
                        self.chunks[0].close()

                    self.chunks.append(chunk)
                    return chunk.get(length)
            except RequestError as e:
                FuseOSError.convert(e)

        def clear(self):
            """Closes chunks and clears chunk deque."""
            with self.lock:
                for chunk in self.chunks:
                    try:
                        chunk.close()
                    except:
                        pass
                self.chunks.clear()

    def get(self, id_, offset, length, total):
        with self.lock:
            f = self.files[id_]
        return f.get(self.acd_client, id_, offset, length, total)

    def invalidate(self):
        pass

    def release(self, id_):
        with self.lock:
            f = self.files.get(id_)
        if f:
            f.clear()


class WriteProxy(object):
    """Collection of WriteStreams for consecutive file write operations."""

    def __init__(self, acd_client, cache, buffer_size, timeout):
        self.acd_client = acd_client
        self.cache = cache
        self.buffers = defaultdict(lambda: WriteProxy.WriteBuffer(buffer_size))

    class WriteBuffer(object):
        def __init__(self, buffer_size):
            self.f = tempfile.SpooledTemporaryFile(max_size=buffer_size)
            self.lock = Lock()

        def write(self, offset, bytes_: bytes):
            with self.lock:
                self.f.seek(0, os.SEEK_END)
                old_len = self.f.tell()
                if offset > old_len:
                    logger.error('Wrong offset for writing to buffer; writing gap detected')
                    raise FuseOSError(errno.ESPIPE)
                self.f.seek(offset)
                self.f.write(bytes_)
                return old_len

        def get_file(self):
            self.f.seek(0)
            return self.f

    def _write_and_sync(self, buffer: WriteBuffer, node_id: str):
        try:
            r = self.acd_client.overwrite_tempfile(node_id, buffer.get_file())
        except (RequestError, IOError) as e:
            logger.error('Error writing node "%s". %s' % (node_id, str(e)))
        else:
            self.cache.insert_node(r)

    def write(self, node_id, fh, offset, bytes_):
        """Gets WriteBuffer from defaultdict.

        :raises: FuseOSError: wrong offset or writing failed"""

        b = self.buffers[node_id]
        b.write(offset, bytes_)

    def release(self, node_id, fh):
        """:raises: FuseOSError"""

        b = self.buffers.get(node_id)
        if b:
            self._write_and_sync(b, node_id)
            del self.buffers[node_id]


class LoggingMixIn(object):
    """Modified fusepy LoggingMixIn that does not log read or written bytes
    and nicely formats non-decimal based arguments."""

    def __call__(self, op, path, *args):
        targs = None
        if op == 'open':
            targs = (('0x%0*x' % (4, args[0]),) + args[1:])
        elif op == 'write':
            targs = (len(args[0]),) + args[1:]
        elif op == 'chmod':
            targs = (oct(args[0]),) + args[1:]

        logger.debug('-> %s %s %s', op, path, repr(args if not targs else targs))

        ret = '[Unhandled Exception]'
        try:
            ret = getattr(self, op)(path, *args)
            return ret
        except OSError as e:
            ret = str(e)
            raise
        finally:
            if op == 'read':
                ret = len(ret)
            logger.debug('<- %s %s', op, repr(ret))


class ACDFuse(LoggingMixIn, Operations):
    """FUSE filesystem operations class for Amazon Cloud Drive.
    See `<http://fuse.sourceforge.net/doxygen/structfuse__operations.html>`_."""

    def __init__(self, **kwargs):
        """Calculates ACD usage and starts autosync process.

        :param kwargs: cache (NodeCache), acd_client (ACDClient), autosync (partial)"""

        self.xattr_cache = {}
        self.xattr_dirty = set()
        self.xattr_cache_lock = Lock()

        self.cache = kwargs['cache']
        self.acd_client = kwargs['acd_client']
        self.acd_client_owner = self.cache.KeyValueStorage.get(CacheConsts.OWNER_ID)
        autosync = kwargs['autosync']
        conf = kwargs['conf']

        self.rp = ReadProxy(self.acd_client,
                            conf.getint('read', 'open_chunk_limit'), conf.getint('read', 'timeout'))
        """collection of files opened for reading"""
        self.wp = WriteProxy(self.acd_client, self.cache,
                             conf.getint('write', 'buffer_size'), conf.getint('write', 'timeout'))
        """collection of files opened for writing"""
        try:
            total, _ = self.acd_client.fs_sizes()
        except RequestError:
            logger.warning('Error getting account quota data. '
                           'Cannot determine total and available disk space.')
            total = 0

        self.total = total
        """total disk space"""
        self.free = 0 if not total else total - self.cache.calculate_usage()
        """manually calculated available disk space"""
        self.fh = 1
        """file handle counter\n\n :type: int"""
        self.handles = {}
        """map fh->node\n\n :type: dict"""
        self.node_to_fh = defaultdict(lambda: set())
        """map node_id to list of interested file handles"""
        self.fh_lock = Lock()
        """lock for fh counter increment and handle dict writes"""
        self.nlinks = kwargs.get('nlinks', False)
        """whether to calculate the number of hardlinks for folders"""

        self.destroyed = autosync.keywords['stop']
        """:type: multiprocessing.Event"""

        p = Process(target=autosync)
        p.start()

    def destroy(self, path):
        self._xattr_write_and_sync()
        self.destroyed.set()

    def readdir(self, path, fh) -> 'List[str]':
        """Lists the path's contents.

        :raises: FuseOSError if path is not a node or path is not a folder"""

        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        if not node.type == 'folder':
            raise FuseOSError(errno.ENOTDIR)

        folders, files = self.cache.list_children(folder_id=node.id, folder_path=path)
        return [_ for _ in ['.', '..'] + [c.name for c in folders + files]]

    def getattr(self, path, fh=None) -> dict:
        """Creates a stat-like attribute dict, see :manpage:`stat(2)`.
        Calculates correct number of links for folders if :attr:`nlinks` is set."""

        if fh:
            node = self.handles[fh]
        else:
            node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)

        try: mtime = self._getxattr(node.id, _XATTR_MTIME_OVERRIDE_NAME)
        except: mtime = node.modified.timestamp()

        times = dict(st_atime=time(),
                     st_mtime=mtime,
                     st_ctime=node.created.timestamp())

        if node.is_folder:
            return dict(st_mode=stat.S_IFDIR | 0o0777,
                        st_nlink=self.cache.num_children(node.id) if self.nlinks else 1,
                        **times)
        elif node.is_file:
            return dict(st_mode=stat.S_IFREG | 0o0666,
                        st_nlink=self.cache.num_parents(node.id) if self.nlinks else 1,
                        st_size=node.size,
                        **times)

    def listxattr(self, path):
        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        return self._listxattr(node.id)

    def _listxattr(self, node_id):
        self._xattr_load(node_id)
        with self.xattr_cache_lock:
            try:
                return [k for k, v in self.xattr_cache[node_id].items()]
            except:
                return []

    def getxattr(self, path, name, position=0):
        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        return self._getxattr_bytes(node.id, name)

    def _getxattr(self, node_id, name):
        self._xattr_load(node_id)
        with self.xattr_cache_lock:
            try:
                ret = self.xattr_cache[node_id][name]
                if ret:
                    return ret
            except:
                raise FuseOSError(errno.ENODATA)  # should be ENOATTR
            else:
                raise FuseOSError(errno.ENODATA)  # should be ENOATTR

    def _getxattr_bytes(self, node_id, name):
        return binascii.a2b_base64(self._getxattr(node_id, name))

    def removexattr(self, path, name):
        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        self._removexattr(node.id, name)

    def _removexattr(self, node_id, name):
        self._xattr_load(node_id)
        with self.xattr_cache_lock:
            if name in self.xattr_cache[node_id]:
                del self.xattr_cache[node_id][name]
                self.properties_dirty.add(node_id)

    def setxattr(self, path, name, value, options, position=0):
        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)
        self._setxattr_bytes(node.id, name, value)

    def _setxattr(self, node_id, name, value):
        self._xattr_load(node_id)
        with self.xattr_cache_lock:
            try:
                self.xattr_cache[node_id][name] = value
                self.xattr_dirty.add(node_id)
            except:
                raise FuseOSError(errno.ENOTSUP)

    def _setxattr_bytes(self, node_id, name, value: bytes):
        self._setxattr(node_id, name, binascii.b2a_base64(value).decode("utf-8"))

    def _xattr_load(self, node_id):
        with self.xattr_cache_lock:
            if node_id not in self.xattr_cache:
                xattrs_str = self.cache.get_property(node_id, self.acd_client_owner, _XATTR_PROPERTY_NAME)
                try: self.xattr_cache[node_id] = json.loads(xattrs_str)
                except: self.xattr_cache[node_id] = {}

    def _xattr_write_and_sync(self):
        with self.xattr_cache_lock:
            for node_id in self.xattr_dirty:
                try:
                    xattrs_str = json.dumps(self.xattr_cache[node_id])
                    self.acd_client.add_property(node_id, self.acd_client_owner, _XATTR_PROPERTY_NAME,
                                                 xattrs_str)
                except (RequestError, IOError) as e:
                    logger.error('Error writing node xattrs "%s". %s' % (node_id, str(e)))
                else:
                    self.cache.insert_property(node_id, self.acd_client_owner, _XATTR_PROPERTY_NAME, xattrs_str)
                    logger.debug('_xattr_write_and_sync: node: %s xattrs: %s: ' % (node_id, xattrs_str))
            self.xattr_dirty.clear()

    def read(self, path, length, offset, fh) -> bytes:
        """Read ```length`` bytes from ``path`` at ``offset``."""

        if fh:
            node = self.handles[fh]
        else:
            node = self.cache.resolve(path, trash=False)
        if not node:
            raise FuseOSError(errno.ENOENT)

        if node.size <= offset:
            return b''

        if node.size < offset + length:
            length = node.size - offset

        return self.rp.get(node.id, offset, length, node.size)

    def statfs(self, path) -> dict:
        """Gets some filesystem statistics as specified in :manpage:`stat(2)`."""

        bs = 512 * 1024  # no effect?
        return dict(f_bsize=bs,
                    f_frsize=bs,
                    f_blocks=self.total // bs,  # total no of blocks
                    f_bfree=self.free // bs,  # free blocks
                    f_bavail=self.free // bs,
                    f_namemax=256
                    )

    def mkdir(self, path, mode):
        """Creates a directory at ``path`` (see :manpage:`mkdir(2)`).

        :param mode: not used"""

        name = os.path.basename(path)
        ppath = os.path.dirname(path)
        p = self.cache.resolve(ppath)
        if not p:
            raise FuseOSError(errno.ENOTDIR)

        try:
            r = self.acd_client.create_folder(name, p.id)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def _trash(self, path):
        logger.debug('trash %s' % path)
        node = self.cache.resolve(path, False)

        if not node:  # or not parent:
            raise FuseOSError(errno.ENOENT)

        try:
            # if len(node.parents) > 1:
            #     r = metadata.remove_child(parent.id, node.id)
            # else:
            r = self.acd_client.move_to_trash(node.id)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def rmdir(self, path):
        """Moves a directory into ACD trash."""
        self._trash(path)

    def unlink(self, path):
        """Moves a file into ACD trash."""
        self._trash(path)

    def create(self, path, mode) -> int:
        """Creates an empty file at ``path``.

        :param mode: not used
        :returns int: file handle"""

        name = os.path.basename(path)
        ppath = os.path.dirname(path)
        p = self.cache.resolve(ppath, False)
        if not p:
            raise FuseOSError(errno.ENOTDIR)

        try:
            r = self.acd_client.create_file(name, p.id)
            self.cache.insert_node(r)
            node = self.cache.get_node(r['id'])
        except RequestError as e:
            FuseOSError.convert(e)

        with self.fh_lock:
            self.fh += 1
            self.handles[self.fh] = node
            self.node_to_fh[node.id].add(self.fh)
        return self.fh

    def rename(self, old, new):
        """Renames ``old`` into ``new`` (may also involve a move).
        If ``new`` is an existing file, it is moved into the ACD trash.

        :raises FuseOSError: ENOENT if ``old`` is not a node, \
        EEXIST if ``new`` is an existing folder \
        ENOTDIR if ``new``'s parent path does not exist"""

        if old == new:
            return

        node = self.cache.resolve(old, False)
        if not node:
            raise FuseOSError(errno.ENOENT)

        new_bn, new_dn = os.path.basename(new), os.path.dirname(new)
        old_bn, old_dn = os.path.basename(old), os.path.dirname(old)

        existing = self.cache.resolve(new, False)
        if existing:
            if existing.is_file:
                self._trash(new)
            else:
                raise FuseOSError(errno.EEXIST)

        if new_bn != old_bn:
            self._rename(node.id, new_bn)

        if new_dn != old_dn:
            # odir_id = self.cache.resolve_path(old_dn, False)
            ndir = self.cache.resolve(new_dn, False)
            if not ndir:
                raise FuseOSError(errno.ENOTDIR)
            self._move(node.id, ndir.id)

    def _rename(self, id, name):
        try:
            r = self.acd_client.rename_node(id, name)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def _move(self, id, new_folder):
        try:
            r = self.acd_client.move_node(id, new_folder)
        except RequestError as e:
            FuseOSError.convert(e)
        else:
            self.cache.insert_node(r)

    def open(self, path, flags) -> int:
        """Opens a file.

        :param flags: flags defined as in :manpage:`open(2)`
        :returns: file handle"""

        if (flags & os.O_APPEND) == os.O_APPEND:
            raise FuseOSError(errno.EFAULT)

        node = self.cache.resolve(path, False)
        if not node:
            raise FuseOSError(errno.ENOENT)
        with self.fh_lock:
            self.fh += 1
            self.handles[self.fh] = node
            self.node_to_fh[node.id].add(self.fh)
        return self.fh

    def write(self, path, data, offset, fh) -> int:
        """Invokes :attr:`wp`'s write function.

        :returns: number of bytes written"""

        node_id = self.handles[fh].id
        self.wp.write(node_id, fh, offset, data)
        return len(data)

    def flush(self, path, fh):
        """noop since we need to keep the whole buffer in memory;
        acd only supports sequentual writes otherwise"""
        pass

    def truncate(self, path, length, fh=None):
        """Pseudo-truncates a file, i.e. clears content if ``length``==0 or does nothing
        if ``length`` is positive.

        :raises FuseOSError: if pseudo-truncation to length is not supported"""

        if fh:
            node = self.handles[fh]
        else:
            node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)

        if length == 0:
            try:
                r = self.acd_client.clear_file(node.id)
            except RequestError as e:
                raise FuseOSError.convert(e)
            else:
                self.cache.insert_node(r)

        """No good way to deal with positive lengths at the moment; since we can only do
        something about it in the middle of writing, this means the only use case we can
        capture is when a program over-writes and then truncates back. In the future, if
        we can get cached file backing instead of memory backing, there would be more to
        do here. In the mean time we ignore."""
        return 0

    def release(self, path, fh):
        """Releases an open ``path``."""

        if fh:
            node = self.handles[fh]
        else:
            node = self.cache.resolve(path, trash=False)
        if node:
            self.rp.release(node.id)
            with self.fh_lock:
                """release the writer if there's no more interest. This allows many file
                handles to write to a single node provided they do it in order, enabling
                sequential writes using mmap.
                """
                interest = self.node_to_fh.get(node.id)
                if interest:
                    interest.discard(fh)
                if not interest:
                    self.wp.release(node.id, fh)
                    self._xattr_write_and_sync()
                    del self.node_to_fh[node.id]
                del self.handles[fh]
        else:
            raise FuseOSError(errno.ENOENT)

    def utimens(self, path, times=None):
        """Should set node atime and mtime to values as passed in ``times``
        or current time (see :manpage:`utimensat(2)`).
        Note that this is only implemented for modified time.

        :param times: [atime, mtime]"""

        node = self.cache.resolve(path)
        if not node:
            raise FuseOSError(errno.ENOENT)

        if times:
            # atime = times[0]
            mtime = times[1]
        else:
            # atime = time()
            mtime = time()

        try:
            self._setxattr(node.id, _XATTR_MTIME_OVERRIDE_NAME, mtime)
            self._xattr_write_and_sync()
        except:
            raise FuseOSError(errno.ENOTSUP)

        return 0

    def chmod(self, path, mode):
        """Not implemented."""
        pass

    def chown(self, path, uid, gid):
        """Not implemented."""
        pass


def mount(path: str, args: dict, **kwargs) -> 'Union[int, None]':
    """Fusermounts Amazon Cloud Drive to specified mountpoint.

    :raises: RuntimeError
    :param args: args to pass on to ACDFuse init
    :param kwargs: fuse mount options as described in :manpage:`fuse(8)`"""

    if not os.path.isdir(path):
        logger.critical('Mountpoint does not exist or already used.')
        return 1

    opts = dict(auto_cache=True, sync_read=True)
    if sys.platform == 'linux':
        opts['big_writes']=True

    kwargs.update(opts)

    args['conf'] = get_conf(args['settings_path'], _SETTINGS_FILENAME, _def_conf)

    FUSE(ACDFuse(**args), path, subtype=ACDFuse.__name__, **kwargs)


def unmount(path=None, lazy=False) -> int:
    """Unmounts a specific mountpoint if path given or all of the user's ACDFuse mounts.

    :returns: 0 on success, 1 on error"""

    import platform
    import re
    import subprocess

    system = platform.system().lower()

    if system != 'darwin':
        umount_cmd = ['fusermount', '-u']
    else:
        umount_cmd = ['umount']
    if lazy:
        if system == 'linux':
            umount_cmd.append('-z')
        else:
            logging.warning('Lazy unmounting is not supported on your platform.')

    fuse_st = ACDFuse.__name__

    if path:
        paths = [path]
    else:
        if system not in ['linux', 'darwin']:
            logger.critical('Automatic unmounting is not supported on your platform.')
            return 1

        paths = []
        try:
            if system == 'linux':
                mounts = subprocess.check_output(['mount', '-t', 'fuse.' + fuse_st])
            elif system == 'darwin':
                mounts = subprocess.check_output(['mount'])

            mounts = mounts.decode('UTF-8').splitlines()
        except:
            logger.critical('Getting mountpoints failed.')
            return 1

        for mount in mounts:
            if fuse_st in mount:
                if (system == 'linux' and 'user_id=%i' % os.getuid() in mount) or \
                (system == 'darwin' and 'mounted by %s' % os.getlogin() in mount):
                    paths.append(re.search(fuse_st + ' on (.*?) ', mount).group(1))

    ret = 0
    for path in paths:
        command = list(umount_cmd)
        command.append(path)
        try:
            subprocess.check_call(command)
        except subprocess.CalledProcessError:
            # logger.error('Unmounting %s failed.' % path)
            ret |= 1

    return ret
