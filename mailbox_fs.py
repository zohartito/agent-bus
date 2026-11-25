"""Private, no-follow mailbox I/O (BUS-07, BUS-08).

Trusted ancestors outside the configured root are resolved once. The root and
all descendants are opened relative to directory descriptors, never through a
symlink. Each operation keeps its parent descriptors until the syscall ends.
"""

from contextlib import contextmanager
import errno
import fnmatch
import os
from pathlib import Path
import stat


class MailboxError(Exception):
    """Unsafe mailbox access; safe to show to the caller."""


class MailboxPath:
    def __init__(self, root, parts=()):
        root = Path(root).expanduser().absolute()
        self.root = root.parent.resolve() / root.name
        self.parts = parts

    def __fspath__(self):
        return str(self.root.joinpath(*self.parts))

    def __str__(self):
        return os.fspath(self)

    def __truediv__(self, name):
        if not isinstance(name, str) or not name or name in ('.', '..') or '/' in name or '\\' in name:
            raise MailboxError('invalid mailbox path component')
        return MailboxPath(self.root, self.parts + (name,))

    def __lt__(self, other):
        return str(self) < str(other)

    @property
    def name(self):
        return self.parts[-1] if self.parts else self.root.name

    @property
    def stem(self):
        return Path(self.name).stem

    @property
    def parent(self):
        if not self.parts:
            raise MailboxError('cannot traverse above mailbox root')
        return MailboxPath(self.root, self.parts[:-1])

    @staticmethod
    def _validate(fd, directory=False, writing=False):
        info = os.fstat(fd)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode):
            raise MailboxError('mailbox entries must be regular files or directories')
        if info.st_uid != os.getuid():
            raise MailboxError('mailbox entry is owned by another user')
        mode = 0o700 if directory else 0o600
        if not directory and info.st_nlink > 1 and (writing or stat.S_IMODE(info.st_mode) != mode):
            raise MailboxError('refusing to modify a multiply linked mailbox file')
        if stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(fd, mode)
        return info

    @staticmethod
    def _open_dir(name, parent):
        try:
            return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as err:
            if err.errno in (errno.ELOOP, errno.ENOTDIR):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise MailboxError('symlink in mailbox path') from err
            raise

    @contextmanager
    def _dir_fd(self, create=False):
        if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
            raise MailboxError('secure mailbox I/O requires POSIX no-follow directory operations')
        if create:
            self.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in (self.root.name, *self.parts):
                if create:
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = self._open_dir(name, fd)
                os.close(fd)
                fd = child
                self._validate(fd, directory=True)
            yield fd
        finally:
            os.close(fd)

    def mkdir(self, parents=False, exist_ok=False):
        # Every ancestor below the root is also validated and made private.
        with self._dir_fd(create=True):
            pass

    def open_fd(self, flags, mode=0o600):
        with self.parent._dir_fd() as parent:
            for attempt in range(3):
                try:
                    # Validate before truncating an existing file, and never block on a FIFO.
                    fd = os.open(self.name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=parent)
                    break
                except OSError as err:
                    # Darwin can report ENOENT when O_CREAT|O_NOFOLLOW races another
                    # creator. Retry against the same held parent with no flags weakened.
                    if err.errno == errno.ENOENT and flags & os.O_CREAT and attempt < 2:
                        continue
                    if err.errno == errno.ELOOP:
                        raise MailboxError('symlink in mailbox path') from err
                    raise
            try:
                self._validate(fd, writing=bool(flags & (os.O_WRONLY | os.O_RDWR)))
                if flags & os.O_TRUNC:
                    os.ftruncate(fd, 0)
                return fd
            except BaseException:
                os.close(fd)
                raise

    def open(self, mode='r', encoding=None):
        flags = os.O_RDWR if '+' in mode else (os.O_RDONLY if mode[0] == 'r' else os.O_WRONLY)
        if mode[0] == 'w':
            flags |= os.O_CREAT | os.O_TRUNC
        elif mode[0] == 'a':
            flags |= os.O_CREAT | os.O_APPEND
        elif mode[0] == 'x':
            flags |= os.O_CREAT | os.O_EXCL
        return os.fdopen(self.open_fd(flags), mode, encoding=encoding)

    def read_text(self, encoding='utf-8'):
        with self.open(encoding=encoding) as handle:
            return handle.read()

    def write_text(self, text, encoding='utf-8'):
        with self.open('w', encoding=encoding) as handle:
            return handle.write(text)

    def touch(self):
        fd = self.open_fd(os.O_WRONLY | os.O_CREAT)
        try:
            os.utime(fd, None)
        finally:
            os.close(fd)

    def stat(self):
        if not self.parts:
            with self._dir_fd() as fd:
                return os.fstat(fd)
        with self.parent._dir_fd() as parent:
            info = os.stat(self.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise MailboxError('symlink in mailbox path')
        if stat.S_ISDIR(info.st_mode):
            with self._dir_fd() as fd:
                return os.fstat(fd)
        fd = self.open_fd(os.O_RDONLY)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def exists(self):
        try:
            self.stat()
            return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    def is_dir(self):
        try:
            with self._dir_fd():
                return True
        except (FileNotFoundError, NotADirectoryError):
            return False

    def is_file(self):
        try:
            return stat.S_ISREG(self.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

    def iterdir(self):
        with self._dir_fd() as fd:
            names = os.listdir(fd)
        return iter(self / name for name in names)

    def glob(self, pattern):
        try:
            return (path for path in self.iterdir() if fnmatch.fnmatchcase(path.name, pattern))
        except FileNotFoundError:
            return iter(())

    def unlink(self, missing_ok=False):
        try:
            with self.parent._dir_fd() as parent:
                os.unlink(self.name, dir_fd=parent)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def replace(self, target):
        with self.parent._dir_fd() as src, target.parent._dir_fd() as dst:
            os.replace(self.name, target.name, src_dir_fd=src, dst_dir_fd=dst)

    def link_to(self, target):
        with self.parent._dir_fd() as src, target.parent._dir_fd() as dst:
            os.link(self.name, target.name, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
