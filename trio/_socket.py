import os
import sys
import select
import socket as _stdlib_socket
from functools import wraps as _wraps
from typing import TYPE_CHECKING

import idna as _idna

import trio
from . import _core


# Usage:
#
#   async with _try_sync():
#       return sync_call_that_might_fail_with_exception()
#   # we only get here if the sync call in fact did fail with a
#   # BlockingIOError
#   return await do_it_properly_with_a_check_point()
#
class _try_sync:
    def __init__(self, blocking_exc_override=None):
        self._blocking_exc_override = blocking_exc_override

    def _is_blocking_io_error(self, exc):
        if self._blocking_exc_override is None:
            return isinstance(exc, BlockingIOError)
        else:
            return self._blocking_exc_override(exc)

    async def __aenter__(self):
        await trio.lowlevel.checkpoint_if_cancelled()

    async def __aexit__(self, etype, value, tb):
        if value is not None and self._is_blocking_io_error(value):
            # Discard the exception and fall through to the code below the
            # block
            return True
        else:
            await trio.lowlevel.cancel_shielded_checkpoint()
            # Let the return or exception propagate
            return False


################################################################
# CONSTANTS
################################################################

try:
    from socket import IPPROTO_IPV6
except ImportError:
    # Before Python 3.8, Windows is missing IPPROTO_IPV6
    # https://bugs.python.org/issue29515
    if sys.platform == "win32":  # pragma: no branch
        IPPROTO_IPV6 = 41

################################################################
# Overrides
################################################################

_resolver = _core.RunVar("hostname_resolver")
_socket_factory = _core.RunVar("socket_factory")


def set_custom_hostname_resolver(hostname_resolver):
    """Set a custom hostname resolver.

    By default, Trio's :func:`getaddrinfo` and :func:`getnameinfo` functions
    use the standard system resolver functions. This function allows you to
    customize that behavior. The main intended use case is for testing, but it
    might also be useful for using third-party resolvers like `c-ares
    <https://c-ares.haxx.se/>`__ (though be warned that these rarely make
    perfect drop-in replacements for the system resolver). See
    :class:`trio.abc.HostnameResolver` for more details.

    Setting a custom hostname resolver affects all future calls to
    :func:`getaddrinfo` and :func:`getnameinfo` within the enclosing call to
    :func:`trio.run`. All other hostname resolution in Trio is implemented in
    terms of these functions.

    Generally you should call this function just once, right at the beginning
    of your program.

    Args:
      hostname_resolver (trio.abc.HostnameResolver or None): The new custom
          hostname resolver, or None to restore the default behavior.

    Returns:
      The previous hostname resolver (which may be None).

    """
    old = _resolver.get(None)
    _resolver.set(hostname_resolver)
    return old


def set_custom_socket_factory(socket_factory):
    """Set a custom socket object factory.

    This function allows you to replace Trio's normal socket class with a
    custom class. This is very useful for testing, and probably a bad idea in
    any other circumstance. See :class:`trio.abc.HostnameResolver` for more
    details.

    Setting a custom socket factory affects all future calls to :func:`socket`
    within the enclosing call to :func:`trio.run`.

    Generally you should call this function just once, right at the beginning
    of your program.

    Args:
      socket_factory (trio.abc.SocketFactory or None): The new custom
          socket factory, or None to restore the default behavior.

    Returns:
      The previous socket factory (which may be None).

    """
    old = _socket_factory.get(None)
    _socket_factory.set(socket_factory)
    return old


################################################################
# getaddrinfo and friends
################################################################

_NUMERIC_ONLY = _stdlib_socket.AI_NUMERICHOST | _stdlib_socket.AI_NUMERICSERV


async def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Look up a numeric address given a name.

    Arguments and return values are identical to :func:`socket.getaddrinfo`,
    except that this version is async.

    Also, :func:`trio.socket.getaddrinfo` correctly uses IDNA 2008 to process
    non-ASCII domain names. (:func:`socket.getaddrinfo` uses IDNA 2003, which
    can give the wrong result in some cases and cause you to connect to a
    different host than the one you intended; see `bpo-17305
    <https://bugs.python.org/issue17305>`__.)

    This function's behavior can be customized using
    :func:`set_custom_hostname_resolver`.

    """

    # If host and port are numeric, then getaddrinfo doesn't block and we can
    # skip the whole thread thing, which seems worthwhile. So we try first
    # with the _NUMERIC_ONLY flags set, and then only spawn a thread if that
    # fails with EAI_NONAME:
    def numeric_only_failure(exc):
        return (
            isinstance(exc, _stdlib_socket.gaierror)
            and exc.errno == _stdlib_socket.EAI_NONAME
        )

    async with _try_sync(numeric_only_failure):
        return _stdlib_socket.getaddrinfo(
            host, port, family, type, proto, flags | _NUMERIC_ONLY
        )
    # That failed; it's a real hostname. We better use a thread.
    #
    # Also, it might be a unicode hostname, in which case we want to do our
    # own encoding using the idna module, rather than letting Python do
    # it. (Python will use the old IDNA 2003 standard, and possibly get the
    # wrong answer - see bpo-17305). However, the idna module is picky, and
    # will refuse to process some valid hostname strings, like "::1". So if
    # it's already ascii, we pass it through; otherwise, we encode it to.
    if isinstance(host, str):
        try:
            host = host.encode("ascii")
        except UnicodeEncodeError:
            # UTS-46 defines various normalizations; in particular, by default
            # idna.encode will error out if the hostname has Capital Letters
            # in it; with uts46=True it will lowercase them instead.
            host = _idna.encode(host, uts46=True)
    hr = _resolver.get(None)
    if hr is not None:
        return await hr.getaddrinfo(host, port, family, type, proto, flags)
    else:
        return await trio.to_thread.run_sync(
            _stdlib_socket.getaddrinfo,
            host,
            port,
            family,
            type,
            proto,
            flags,
            cancellable=True,
        )


async def getnameinfo(sockaddr, flags):
    """Look up a name given a numeric address.

    Arguments and return values are identical to :func:`socket.getnameinfo`,
    except that this version is async.

    This function's behavior can be customized using
    :func:`set_custom_hostname_resolver`.

    """
    hr = _resolver.get(None)
    if hr is not None:
        return await hr.getnameinfo(sockaddr, flags)
    else:
        return await trio.to_thread.run_sync(
            _stdlib_socket.getnameinfo, sockaddr, flags, cancellable=True
        )


async def getprotobyname(name):
    """Look up a protocol number by name. (Rarely used.)

    Like :func:`socket.getprotobyname`, but async.

    """
    return await trio.to_thread.run_sync(
        _stdlib_socket.getprotobyname, name, cancellable=True
    )


# obsolete gethostbyname etc. intentionally omitted
# likewise for create_connection (use open_tcp_stream instead)

################################################################
# Socket "constructors"
################################################################


def from_stdlib_socket(sock):
    """Convert a standard library :class:`socket.socket` object into a Trio
    socket object.

    """
    return _SocketType(sock)


@_wraps(_stdlib_socket.fromfd, assigned=(), updated=())
def fromfd(fd, family, type, proto=0):
    """Like :func:`socket.fromfd`, but returns a Trio socket object."""
    family, type, proto = _sniff_sockopts_for_fileno(family, type, proto, fd)
    return from_stdlib_socket(_stdlib_socket.fromfd(fd, family, type, proto))


if sys.platform == "win32" or (
    not TYPE_CHECKING and hasattr(_stdlib_socket, "fromshare")
):

    @_wraps(_stdlib_socket.fromshare, assigned=(), updated=())
    def fromshare(*args, **kwargs):
        return from_stdlib_socket(_stdlib_socket.fromshare(*args, **kwargs))


@_wraps(_stdlib_socket.socketpair, assigned=(), updated=())
def socketpair(*args, **kwargs):
    """Like :func:`socket.socketpair`, but returns a pair of Trio socket
    objects.

    """
    left, right = _stdlib_socket.socketpair(*args, **kwargs)
    return (from_stdlib_socket(left), from_stdlib_socket(right))


@_wraps(_stdlib_socket.socket, assigned=(), updated=())
def socket(
    family=_stdlib_socket.AF_INET,
    type=_stdlib_socket.SOCK_STREAM,
    proto=0,
    fileno=None,
):
    """Create a new Trio socket, like :class:`socket.socket`.

    This function's behavior can be customized using
    :func:`set_custom_socket_factory`.

    """
    if fileno is None:
        sf = _socket_factory.get(None)
        if sf is not None:
            return sf.socket(family, type, proto)
    else:
        family, type, proto = _sniff_sockopts_for_fileno(family, type, proto, fileno)
    stdlib_socket = _stdlib_socket.socket(family, type, proto, fileno)
    return from_stdlib_socket(stdlib_socket)


def _sniff_sockopts_for_fileno(family, type, proto, fileno):
    """Correct SOCKOPTS for given fileno, falling back to provided values."""
    # Wrap the raw fileno into a Python socket object
    # This object might have the wrong metadata, but it lets us easily call getsockopt
    # and then we'll throw it away and construct a new one with the correct metadata.
    if sys.platform != "linux":
        return family, type, proto
    from socket import SO_DOMAIN, SO_PROTOCOL, SOL_SOCKET, SO_TYPE

    sockobj = _stdlib_socket.socket(family, type, proto, fileno=fileno)
    try:
        family = sockobj.getsockopt(SOL_SOCKET, SO_DOMAIN)
        proto = sockobj.getsockopt(SOL_SOCKET, SO_PROTOCOL)
        type = sockobj.getsockopt(SOL_SOCKET, SO_TYPE)
    finally:
        # Unwrap it again, so that sockobj.__del__ doesn't try to close our socket
        sockobj.detach()
    return family, type, proto


################################################################
# _SocketType
################################################################

# sock.type gets weird stuff set in it, in particular on Linux:
#
#   https://bugs.python.org/issue21327
#
# But on other platforms (e.g. Windows) SOCK_NONBLOCK and SOCK_CLOEXEC aren't
# even defined. To recover the actual socket type (e.g. SOCK_STREAM) from a
# socket.type attribute, mask with this:
_SOCK_TYPE_MASK = ~(
    getattr(_stdlib_socket, "SOCK_NONBLOCK", 0)
    | getattr(_stdlib_socket, "SOCK_CLOEXEC", 0)
)


def _make_simple_sock_method_wrapper(methname, wait_fn, maybe_avail=False):
    fn = getattr(_stdlib_socket.socket, methname)

    @_wraps(fn, assigned=("__name__",), updated=())
    async def wrapper(self, *args, **kwargs):
        return await self._nonblocking_helper(fn, args, kwargs, wait_fn)

    wrapper.__doc__ = f"""Like :meth:`socket.socket.{methname}`, but async.

            """
    if maybe_avail:
        wrapper.__doc__ += (
            f"Only available on platforms where :meth:`socket.socket.{methname}` is "
            "available."
        )
    return wrapper


# Helpers to work with the (hostname, port) language that Python uses for socket
# addresses everywhere. Split out into a standalone function so it can be reused by
# FakeNet.


# Take an address in Python's representation, and returns a new address in
# the same representation, but with names resolved to numbers,
# etc.
#
# local=True means that the address is being used with bind() or similar
# local=False means that the address is being used with connect() or sendto() or
# similar.
#
# NOTE: this function does not always checkpoint
async def _resolve_address_nocp(type, family, proto, *, ipv6_v6only, address, local):
    # Do some pre-checking (or exit early for non-IP sockets)
    if family == _stdlib_socket.AF_INET:
        if not isinstance(address, tuple) or not len(address) == 2:
            raise ValueError("address should be a (host, port) tuple")
    elif family == _stdlib_socket.AF_INET6:
        if not isinstance(address, tuple) or not 2 <= len(address) <= 4:
            raise ValueError(
                "address should be a (host, port, [flowinfo, [scopeid]]) tuple"
            )
    elif family == _stdlib_socket.AF_UNIX:
        # unwrap path-likes
        return os.fspath(address)
    else:
        return address

    # -- From here on we know we have IPv4 or IPV6 --
    host, port, *_ = address
    # Fast path for the simple case: already-resolved IP address,
    # already-resolved port. This is particularly important for UDP, since
    # every sendto call goes through here.
    if isinstance(port, int):
        try:
            _stdlib_socket.inet_pton(family, address[0])
        except (OSError, TypeError):
            pass
        else:
            return address
    # Special cases to match the stdlib, see gh-277
    if host == "":
        host = None
    if host == "<broadcast>":
        host = "255.255.255.255"
    flags = 0
    if local:
        flags |= _stdlib_socket.AI_PASSIVE
    # Since we always pass in an explicit family here, AI_ADDRCONFIG
    # doesn't add any value -- if we have no ipv6 connectivity and are
    # working with an ipv6 socket, then things will break soon enough! And
    # if we do enable it, then it makes it impossible to even run tests
    # for ipv6 address resolution on travis-ci, which as of 2017-03-07 has
    # no ipv6.
    # flags |= AI_ADDRCONFIG
    if family == _stdlib_socket.AF_INET6 and not ipv6_v6only:
        flags |= _stdlib_socket.AI_V4MAPPED
    gai_res = await getaddrinfo(host, port, family, type, proto, flags)
    # AFAICT from the spec it's not possible for getaddrinfo to return an
    # empty list.
    assert len(gai_res) >= 1
    # Address is the last item in the first entry
    (*_, normed), *_ = gai_res
    # The above ignored any flowid and scopeid in the passed-in address,
    # so restore them if present:
    if family == _stdlib_socket.AF_INET6:
        normed = list(normed)
        assert len(normed) == 4
        if len(address) >= 3:
            normed[2] = address[2]
        if len(address) >= 4:
            normed[3] = address[3]
        normed = tuple(normed)
    return normed


class SocketType:
    def __init__(self):
        raise TypeError(
            "SocketType is an abstract class; use trio.socket.socket if you "
            "want to construct a socket object"
        )


class _SocketType(SocketType):
    def __init__(self, sock):
        if type(sock) is not _stdlib_socket.socket:
            # For example, ssl.SSLSocket subclasses socket.socket, but we
            # certainly don't want to blindly wrap one of those.
            raise TypeError(
                "expected object of type 'socket.socket', not '{}".format(
                    type(sock).__name__
                )
            )
        self._sock = sock
        self._sock.setblocking(False)
        self._did_shutdown_SHUT_WR = False

    ################################################################
    # Simple + portable methods and attributes
    ################################################################

    # NB this doesn't work because for loops don't create a scope
    # for _name in [
    #         ]:
    #     _meth = getattr(_stdlib_socket.socket, _name)
    #     @_wraps(_meth, assigned=("__name__", "__doc__"), updated=())
    #     def _wrapped(self, *args, **kwargs):
    #         return getattr(self._sock, _meth)(*args, **kwargs)
    #     locals()[_meth] = _wrapped
    # del _name, _meth, _wrapped

    _forward = {
        "detach",
        "get_inheritable",
        "set_inheritable",
        "fileno",
        "getpeername",
        "getsockname",
        "getsockopt",
        "setsockopt",
        "listen",
        "share",
    }

    def __getattr__(self, name):
        if name in self._forward:
            return getattr(self._sock, name)
        raise AttributeError(name)

    def __dir__(self):
        return super().__dir__() + list(self._forward)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._sock.__exit__(*exc_info)

    @property
    def family(self):
        return self._sock.family

    @property
    def type(self):
        return self._sock.type

    @property
    def proto(self):
        return self._sock.proto

    @property
    def did_shutdown_SHUT_WR(self):
        return self._did_shutdown_SHUT_WR

    def __repr__(self):
        return repr(self._sock).replace("socket.socket", "trio.socket.socket")

    def dup(self):
        """Same as :meth:`socket.socket.dup`."""
        return _SocketType(self._sock.dup())

    def close(self):
        if self._sock.fileno() != -1:
            trio.lowlevel.notify_closing(self._sock)
            self._sock.close()

    async def bind(self, address):
        address = await self._resolve_address_nocp(address, local=True)
        if (
            hasattr(_stdlib_socket, "AF_UNIX")
            and self.family == _stdlib_socket.AF_UNIX
            and address[0]
        ):
            # Use a thread for the filesystem traversal (unless it's an
            # abstract domain socket)
            return await trio.to_thread.run_sync(self._sock.bind, address)
        else:
            # POSIX actually says that bind can return EWOULDBLOCK and
            # complete asynchronously, like connect. But in practice AFAICT
            # there aren't yet any real systems that do this, so we'll worry
            # about it when it happens.
            await trio.lowlevel.checkpoint()
            return self._sock.bind(address)

    def shutdown(self, flag):
        # no need to worry about return value b/c always returns None:
        self._sock.shutdown(flag)
        # only do this if the call succeeded:
        if flag in [_stdlib_socket.SHUT_WR, _stdlib_socket.SHUT_RDWR]:
            self._did_shutdown_SHUT_WR = True

    def is_readable(self):
        # use select.select on Windows, and select.poll everywhere else
        if sys.platform == "win32":
            rready, _, _ = select.select([self._sock], [], [], 0)
            return bool(rready)
        p = select.poll()
        p.register(self._sock, select.POLLIN)
        return bool(p.poll(0))

    async def wait_writable(self):
        await _core.wait_writable(self._sock)

    async def _resolve_address_nocp(self, address, *, local):
        if self.family == _stdlib_socket.AF_INET6:
            ipv6_v6only = self._sock.getsockopt(
                IPPROTO_IPV6, _stdlib_socket.IPV6_V6ONLY
            )
        else:
            ipv6_v6only = False
        return await _resolve_address_nocp(
            self.type,
            self.family,
            self.proto,
            ipv6_v6only=ipv6_v6only,
            address=address,
            local=local,
        )

    async def _nonblocking_helper(self, fn, args, kwargs, wait_fn):
        # We have to reconcile two conflicting goals:
        # - We want to make it look like we always blocked in doing these
        #   operations. The obvious way is to always do an IO wait before
        #   calling the function.
        # - But, we also want to provide the correct semantics, and part
        #   of that means giving correct errors. So, for example, if you
        #   haven't called .listen(), then .accept() raises an error
        #   immediately. But in this same circumstance, then on macOS, the
        #   socket does not register as readable. So if we block waiting
        #   for read *before* we call accept, then we'll be waiting
        #   forever instead of properly raising an error. (On Linux,
        #   interestingly, AFAICT a socket that can't possible read/write
        #   *does* count as readable/writable for select() purposes. But
        #   not on macOS.)
        #
        # So, we have to call the function once, with the appropriate
        # cancellation/yielding sandwich if it succeeds, and if it gives
        # BlockingIOError *then* we fall back to IO wait.
        #
        # XX think if this can be combined with the similar logic for IOCP
        # submission...
        async with _try_sync():
            return fn(self._sock, *args, **kwargs)
        # First attempt raised BlockingIOError:
        while True:
            await wait_fn(self._sock)
            try:
                return fn(self._sock, *args, **kwargs)
            except BlockingIOError:
                pass

    ################################################################
    # accept
    ################################################################

    _accept = _make_simple_sock_method_wrapper("accept", _core.wait_readable)

    async def accept(self):
        """Like :meth:`socket.socket.accept`, but async."""
        sock, addr = await self._accept()
        return from_stdlib_socket(sock), addr

    ################################################################
    # connect
    ################################################################

    async def connect(self, address):
        # nonblocking connect is weird -- you call it to start things
        # off, then the socket becomes writable as a completion
        # notification. This means it isn't really cancellable... we close the
        # socket if cancelled, to avoid confusion.
        try:
            address = await self._resolve_address_nocp(address, local=False)
            async with _try_sync():
                # An interesting puzzle: can a non-blocking connect() return EINTR
                # (= raise InterruptedError)? PEP 475 specifically left this as
                # the one place where it lets an InterruptedError escape instead
                # of automatically retrying. This is based on the idea that EINTR
                # from connect means that the connection was already started, and
                # will continue in the background. For a blocking connect, this
                # sort of makes sense: if it returns EINTR then the connection
                # attempt is continuing in the background, and on many system you
                # can't then call connect() again because there is already a
                # connect happening. See:
                #
                #   http://www.madore.org/~david/computers/connect-intr.html
                #
                # For a non-blocking connect, it doesn't make as much sense --
                # surely the interrupt didn't happen after we successfully
                # initiated the connect and are just waiting for it to complete,
                # because a non-blocking connect does not wait! And the spec
                # describes the interaction between EINTR/blocking connect, but
                # doesn't have anything useful to say about non-blocking connect:
                #
                #   http://pubs.opengroup.org/onlinepubs/007904975/functions/connect.html
                #
                # So we have a conundrum: if EINTR means that the connect() hasn't
                # happened (like it does for essentially every other syscall),
                # then InterruptedError should be caught and retried. If EINTR
                # means that the connect() has successfully started, then
                # InterruptedError should be caught and ignored. Which should we
                # do?
                #
                # In practice, the resolution is probably that non-blocking
                # connect simply never returns EINTR, so the question of how to
                # handle it is moot.  Someone spelunked macOS/FreeBSD and
                # confirmed this is true there:
                #
                #   https://stackoverflow.com/questions/14134440/eintr-and-non-blocking-calls
                #
                # and exarkun seems to think it's true in general of non-blocking
                # calls:
                #
                #   https://twistedmatrix.com/pipermail/twisted-python/2010-September/022864.html
                # (and indeed, AFAICT twisted doesn't try to handle
                # InterruptedError).
                #
                # So we don't try to catch InterruptedError. This way if it
                # happens, someone will hopefully tell us, and then hopefully we
                # can investigate their system to figure out what its semantics
                # are.
                return self._sock.connect(address)
            # It raised BlockingIOError, meaning that it's started the
            # connection attempt. We wait for it to complete:
            await _core.wait_writable(self._sock)
        except trio.Cancelled:
            # We can't really cancel a connect, and the socket is in an
            # indeterminate state. Better to close it so we don't get
            # confused.
            self._sock.close()
            raise
        # Okay, the connect finished, but it might have failed:
        err = self._sock.getsockopt(_stdlib_socket.SOL_SOCKET, _stdlib_socket.SO_ERROR)
        if err != 0:
            raise OSError(err, f"Error connecting to {address}: {os.strerror(err)}")

    ################################################################
    # recv
    ################################################################

    recv = _make_simple_sock_method_wrapper("recv", _core.wait_readable)

    ################################################################
    # recv_into
    ################################################################

    recv_into = _make_simple_sock_method_wrapper("recv_into", _core.wait_readable)

    ################################################################
    # recvfrom
    ################################################################

    recvfrom = _make_simple_sock_method_wrapper("recvfrom", _core.wait_readable)

    ################################################################
    # recvfrom_into
    ################################################################

    recvfrom_into = _make_simple_sock_method_wrapper(
        "recvfrom_into", _core.wait_readable
    )

    ################################################################
    # recvmsg
    ################################################################

    if hasattr(_stdlib_socket.socket, "recvmsg"):
        recvmsg = _make_simple_sock_method_wrapper(
            "recvmsg", _core.wait_readable, maybe_avail=True
        )

    ################################################################
    # recvmsg_into
    ################################################################

    if hasattr(_stdlib_socket.socket, "recvmsg_into"):
        recvmsg_into = _make_simple_sock_method_wrapper(
            "recvmsg_into", _core.wait_readable, maybe_avail=True
        )

    ################################################################
    # send
    ################################################################

    send = _make_simple_sock_method_wrapper("send", _core.wait_writable)

    ################################################################
    # sendto
    ################################################################

    @_wraps(_stdlib_socket.socket.sendto, assigned=(), updated=())
    async def sendto(self, *args):
        """Similar to :meth:`socket.socket.sendto`, but async."""
        # args is: data[, flags], address)
        # and kwargs are not accepted
        args = list(args)
        args[-1] = await self._resolve_address_nocp(args[-1], local=False)
        return await self._nonblocking_helper(
            _stdlib_socket.socket.sendto, args, {}, _core.wait_writable
        )

    ################################################################
    # sendmsg
    ################################################################

    if sys.platform != "win32" or (
        not TYPE_CHECKING and hasattr(_stdlib_socket.socket, "sendmsg")
    ):

        @_wraps(_stdlib_socket.socket.sendmsg, assigned=(), updated=())
        async def sendmsg(self, *args):
            """Similar to :meth:`socket.socket.sendmsg`, but async.

            Only available on platforms where :meth:`socket.socket.sendmsg` is
            available.

            """
            # args is: buffers[, ancdata[, flags[, address]]]
            # and kwargs are not accepted
            if len(args) == 4 and args[-1] is not None:
                args = list(args)
                args[-1] = await self._resolve_address_nocp(args[-1], local=False)
            return await self._nonblocking_helper(
                _stdlib_socket.socket.sendmsg, args, {}, _core.wait_writable
            )

    ################################################################
    # sendfile
    ################################################################

    # Not implemented yet:
    # async def sendfile(self, file, offset=0, count=None):
    #     XX

    # Intentionally omitted:
    #   sendall
    #   makefile
    #   setblocking/getblocking
    #   settimeout/gettimeout
    #   timeout
