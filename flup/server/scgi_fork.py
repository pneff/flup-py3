# Copyright (c) 2005 Allan Saddi <allan@saddi.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
# $Id$

"""
scgi - an SCGI/WSGI gateway. (I might have to rename this module.)

For more information about SCGI and mod_scgi for Apache1/Apache2, see
<http://www.mems-exchange.org/software/scgi/>.

For more information about the Web Server Gateway Interface, see
<http://www.python.org/peps/pep-0333.html>.

Example usage:

  #!/usr/bin/env python
  import sys
  from myapplication import app # Assume app is your WSGI application object
  from scgi import WSGIServer
  ret = WSGIServer(app).run()
  sys.exit(ret and 42 or 0)

See the documentation for WSGIServer for more information.

About the bit of logic at the end:
Upon receiving SIGHUP, the python script will exit with status code 42. This
can be used by a wrapper script to determine if the python script should be
re-run. When a SIGINT or SIGTERM is received, the script exits with status
code 0, possibly indicating a normal exit.

Example wrapper script:

  #!/bin/sh
  STATUS=42
  while test $STATUS -eq 42; do
    python "$@" that_script_above.py
    STATUS=$?
  done
"""

__author__ = 'Allan Saddi <allan@saddi.com>'
__version__ = '$Revision$'

import sys
import logging
import socket
import select
import errno
import cStringIO as StringIO
import signal
import datetime
import prefork

__all__ = ['WSGIServer']

# The main classes use this name for logging.
LoggerName = 'scgi-wsgi'

# Set up module-level logger.
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
console.setFormatter(logging.Formatter('%(asctime)s : %(message)s',
                                       '%Y-%m-%d %H:%M:%S'))
logging.getLogger(LoggerName).addHandler(console)
del console

class ProtocolError(Exception):
    """
    Exception raised when the server does something unexpected or
    sends garbled data. Usually leads to a Connection closing.
    """
    pass

def recvall(sock, length):
    """
    Attempts to receive length bytes from a socket, blocking if necessary.
    (Socket may be blocking or non-blocking.)
    """
    dataList = []
    recvLen = 0
    while length:
        try:
            data = sock.recv(length)
        except socket.error, e:
            if e[0] == errno.EAGAIN:
                select.select([sock], [], [])
                continue
            else:
                raise
        if not data: # EOF
            break
        dataList.append(data)
        dataLen = len(data)
        recvLen += dataLen
        length -= dataLen
    return ''.join(dataList), recvLen

def readNetstring(sock):
    """
    Attempt to read a netstring from a socket.
    """
    # First attempt to read the length.
    size = ''
    while True:
        try:
            c = sock.recv(1)
        except socket.error, e:
            if e[0] == errno.EAGAIN:
                select.select([sock], [], [])
                continue
            else:
                raise
        if c == ':':
            break
        if not c:
            raise EOFError
        size += c

    # Try to decode the length.
    try:
        size = int(size)
        if size < 0:
            raise ValueError
    except ValueError:
        raise ProtocolError, 'invalid netstring length'

    # Now read the string.
    s, length = recvall(sock, size)

    if length < size:
        raise EOFError

    # Lastly, the trailer.
    trailer, length = recvall(sock, 1)

    if length < 1:
        raise EOFError

    if trailer != ',':
        raise ProtocolError, 'invalid netstring trailer'

    return s

class StdoutWrapper(object):
    """
    Wrapper for sys.stdout so we know if data has actually been written.
    """
    def __init__(self, stdout):
        self._file = stdout
        self.dataWritten = False

    def write(self, data):
        if data:
            self.dataWritten = True
        self._file.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._file, name)

class Request(object):
    """
    Encapsulates data related to a single request.

    Public attributes:
      environ - Environment variables from web server.
      stdin - File-like object representing the request body.
      stdout - File-like object for writing the response.
    """
    def __init__(self, conn, environ, input, output):
        self._conn = conn
        self.environ = environ
        self.stdin = input
        self.stdout = StdoutWrapper(output)

        self.logger = logging.getLogger(LoggerName)

    def run(self):
        self.logger.info('%s %s%s',
                         self.environ['REQUEST_METHOD'],
                         self.environ.get('SCRIPT_NAME', ''),
                         self.environ.get('PATH_INFO', ''))

        start = datetime.datetime.now()

        try:
            self._conn.server.handler(self)
        except:
            self.logger.exception('Exception caught from handler')
            if not self.stdout.dataWritten:
                self._conn.server.error(self)

        end = datetime.datetime.now()

        handlerTime = end - start
        self.logger.debug('%s %s%s done (%.3f secs)',
                          self.environ['REQUEST_METHOD'],
                          self.environ.get('SCRIPT_NAME', ''),
                          self.environ.get('PATH_INFO', ''),
                          handlerTime.seconds +
                          handlerTime.microseconds / 1000000.0)

class Connection(object):
    """
    Represents a single client (web server) connection. A single request
    is handled, after which the socket is closed.
    """
    def __init__(self, sock, addr, server):
        self._sock = sock
        self._addr = addr
        self.server = server

        self.logger = logging.getLogger(LoggerName)

    def run(self):
        self.logger.debug('Connection starting up (%s:%d)',
                          self._addr[0], self._addr[1])

        try:
            self.processInput()
        except EOFError:
            pass
        except ProtocolError, e:
            self.logger.error("Protocol error '%s'", str(e))
        except:
            self.logger.exception('Exception caught in Connection')

        self.logger.debug('Connection shutting down (%s:%d)',
                          self._addr[0], self._addr[1])

        # All done!
        self._sock.close()

    def processInput(self):
        # Read headers
        headers = readNetstring(self._sock)
        headers = headers.split('\x00')[:-1]
        if len(headers) % 2 != 0:
            raise ProtocolError, 'invalid headers'
        environ = {}
        for i in range(len(headers) / 2):
            environ[headers[2*i]] = headers[2*i+1]

        clen = environ.get('CONTENT_LENGTH')
        if clen is None:
            raise ProtocolError, 'missing CONTENT_LENGTH'
        try:
            clen = int(clen)
            if clen < 0:
                raise ValueError
        except ValueError:
            raise ProtocolError, 'invalid CONTENT_LENGTH'

        self._sock.setblocking(1)
        if clen:
            input = self._sock.makefile('r')
        else:
            # Empty input.
            input = StringIO.StringIO()

        # stdout
        output = self._sock.makefile('w')

        # Allocate Request
        req = Request(self, environ, input, output)

        # Run it.
        req.run()

        output.close()
        input.close()

class WSGIServer(prefork.PreforkServer):
    """
    SCGI/WSGI server. For information about SCGI (Simple Common Gateway
    Interface), see <http://www.mems-exchange.org/software/scgi/>.

    This server is similar to SWAP <http://www.idyll.org/~t/www-tools/wsgi/>,
    another SCGI/WSGI server.

    It differs from SWAP in that it isn't based on scgi.scgi_server and
    therefore, it allows me to implement concurrency using threads. (Also,
    this server was written from scratch and really has no other depedencies.)
    Which server to use really boils down to whether you want multithreading
    or forking. (But as an aside, I've found scgi.scgi_server's implementation
    of preforking to be quite superior. So if your application really doesn't
    mind running in multiple processes, go use SWAP. ;)
    """
    # What Request class to use.
    requestClass = Request

    def __init__(self, application, environ=None,
                 bindAddress=('localhost', 4000), allowedServers=None,
                 loggingLevel=logging.INFO, **kw):
        """
        environ, which must be a dictionary, can contain any additional
        environment variables you want to pass to your application.

        bindAddress is the address to bind to, which must be a tuple of
        length 2. The first element is a string, which is the host name
        or IPv4 address of a local interface. The 2nd element is the port
        number.

        allowedServers must be None or a list of strings representing the
        IPv4 addresses of servers allowed to connect. None means accept
        connections from anywhere.

        loggingLevel sets the logging level of the module-level logger.

        Any additional keyword arguments are passed to the underlying
        ThreadPool.
        """
        if kw.has_key('jobClass'):
            del kw['jobClass']
        if kw.has_key('jobArgs'):
            del kw['jobArgs']
        super(WSGIServer, self).__init__(jobClass=Connection,
                                         jobArgs=(self,), **kw)

        if environ is None:
            environ = {}

        self.application = application
        self.environ = environ
        self._bindAddress = bindAddress
        self._allowedServers = allowedServers

        self.logger = logging.getLogger(LoggerName)
        self.logger.setLevel(loggingLevel)

    def _setupSocket(self):
        """Creates and binds the socket for communication with the server."""
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(self._bindAddress)
        sock.listen(socket.SOMAXCONN)
        return sock

    def _cleanupSocket(self, sock):
        """Closes the main socket."""
        sock.close()

    def _isClientAllowed(self, addr):
        ret = self._allowedServers is None or addr[0] in self._allowedServers
        if not ret:
            self.logger.warning('Server connection from %s disallowed',
                                addr[0])
        return ret

    def run(self):
        """
        Main loop. Call this after instantiating WSGIServer. SIGHUP, SIGINT,
        SIGQUIT, SIGTERM cause it to cleanup and return. (If a SIGHUP
        is caught, this method returns True. Returns False otherwise.)
        """
        self.logger.info('%s starting up', self.__class__.__name__)

        try:
            sock = self._setupSocket()
        except socket.error, e:
            self.logger.error('Failed to bind socket (%s), exiting', e[1])
            return False

        ret = super(WSGIServer, self).run(sock)

        self._cleanupSocket(sock)

        self.logger.info('%s shutting down', self.__class__.__name__)

        return ret

    def handler(self, request):
        """
        WSGI handler. Sets up WSGI environment, calls the application,
        and sends the application's response.
        """
        environ = request.environ
        environ.update(self.environ)

        environ['wsgi.version'] = (1,0)
        environ['wsgi.input'] = request.stdin
        environ['wsgi.errors'] = sys.stderr
        environ['wsgi.multithread'] = False
        environ['wsgi.multiprocess'] = True
        environ['wsgi.run_once'] = False

        if environ.get('HTTPS', 'off') in ('on', '1'):
            environ['wsgi.url_scheme'] = 'https'
        else:
            environ['wsgi.url_scheme'] = 'http'

        headers_set = []
        headers_sent = []
        result = None

        def write(data):
            assert type(data) is str, 'write() argument must be string'
            assert headers_set, 'write() before start_response()'

            if not headers_sent:
                status, responseHeaders = headers_sent[:] = headers_set
                found = False
                for header,value in responseHeaders:
                    if header.lower() == 'content-length':
                        found = True
                        break
                if not found and result is not None:
                    try:
                        if len(result) == 1:
                            responseHeaders.append(('Content-Length',
                                                    str(len(data))))
                    except:
                        pass
                s = 'Status: %s\r\n' % status
                for header in responseHeaders:
                    s += '%s: %s\r\n' % header
                s += '\r\n'
                try:
                    request.stdout.write(s)
                except socket.error, e:
                    if e[0] != errno.EPIPE:
                        raise

            try:
                request.stdout.write(data)
                request.stdout.flush()
            except socket.error, e:
                if e[0] != errno.EPIPE:
                    raise

        def start_response(status, response_headers, exc_info=None):
            if exc_info:
                try:
                    if headers_sent:
                        # Re-raise if too late
                        raise exc_info[0], exc_info[1], exc_info[2]
                finally:
                    exc_info = None # avoid dangling circular ref
            else:
                assert not headers_set, 'Headers already set!'

            assert type(status) is str, 'Status must be a string'
            assert len(status) >= 4, 'Status must be at least 4 characters'
            assert int(status[:3]), 'Status must begin with 3-digit code'
            assert status[3] == ' ', 'Status must have a space after code'
            assert type(response_headers) is list, 'Headers must be a list'
            if __debug__:
                for name,val in response_headers:
                    assert type(name) is str, 'Header names must be strings'
                    assert type(val) is str, 'Header values must be strings'

            headers_set[:] = [status, response_headers]
            return write

        result = self.application(environ, start_response)
        try:
            for data in result:
                if data:
                    write(data)
            if not headers_sent:
                write('') # in case body was empty
        finally:
            if hasattr(result, 'close'):
                result.close()

    def error(self, request):
        """
        Override to provide custom error handling. Ideally, however,
        all errors should be caught at the application level.
        """
        import cgitb
        request.stdout.write('Content-Type: text/html\r\n\r\n' +
                             cgitb.html(sys.exc_info()))

if __name__ == '__main__':
    def test_app(environ, start_response):
        """Probably not the most efficient example."""
        import cgi
        start_response('200 OK', [('Content-Type', 'text/html')])
        yield '<html><head><title>Hello World!</title></head>\n' \
              '<body>\n' \
              '<p>Hello World!</p>\n' \
              '<table border="1">'
        names = environ.keys()
        names.sort()
        for name in names:
            yield '<tr><td>%s</td><td>%s</td></tr>\n' % (
                name, cgi.escape(`environ[name]`))

        form = cgi.FieldStorage(fp=environ['wsgi.input'], environ=environ,
                                keep_blank_values=1)
        if form.list:
            yield '<tr><th colspan="2">Form data</th></tr>'

        for field in form.list:
            yield '<tr><td>%s</td><td>%s</td></tr>\n' % (
                field.name, field.value)

        yield '</table>\n' \
              '</body></html>\n'

    WSGIServer(test_app,
               loggingLevel=logging.DEBUG).run()
