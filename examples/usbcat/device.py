#!/usr/bin/env python -u
# This file is part of python-functionfs
# Copyright (C) 2016  Vincent Pelletier <plr.vincent@gmail.com>
#
# python-functionfs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# python-functionfs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with python-functionfs.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import print_function
import errno
import fcntl
import functools
import os
import select
import sys
import functionfs
import functionfs.ch9
import libaio

# More than one, so we may process one while kernel fills the other.
PENDING_READ_COUNT = 2
# Large-ish buffer, to tolerate bursts without becoming a context switch storm.
BUF_SIZE = 1024 * 1024

trace = functools.partial(print, file=sys.stderr)

class USBCat(functionfs.Function):
    _enabled = False

    def __init__(self, path, writer):
        self._aio_context = libaio.AIOContext(PENDING_READ_COUNT)
        self.eventfd = eventfd = libaio.EventFD()
        self._writer = writer
        fs_list, hs_list, ss_list = functionfs.getInterfaceInAllSpeeds(
            interface={
                'bInterfaceClass': functionfs.ch9.USB_CLASS_VENDOR_SPEC,
                'iInterface': 1,
            },
            endpoint_list=[
                {
                    'endpoint': {
                        'bEndpointAddress': 1 | functionfs.ch9.USB_DIR_IN,
                        'bmAttributes': functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    },
                }, {
                    'endpoint': {
                        'bEndpointAddress': 2 | functionfs.ch9.USB_DIR_OUT,
                        'bmAttributes': functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    },
                },
            ],
        )
        super(USBCat, self).__init__(
            path,
            fs_list=fs_list,
            hs_list=hs_list,
            ss_list=ss_list,
            lang_dict={
                0x0409: [
                    u"USBCat",
                ],
            }
        )
        to_host = self.getEndpoint(2)
        self._aio_recv_block_list = [
            libaio.AIOBlock(
                libaio.AIOBLOCK_MODE_READ,
                to_host,
                [bytearray(BUF_SIZE)],
                0,
                eventfd,
                self._onReceived,
            )
            for _ in xrange(PENDING_READ_COUNT)
        ]
        self.write = self.getEndpoint(1).write

    def close(self):
        self._disable()
        self._aio_context.close()
        super(USBCat, self).close()

    def onBind(self):
        """
        Just for tracing purposes.
        """
        trace('onBind')

    def onUnbind(self):
        """
        Kernel may unbind us without calling disable.
        Still, it does cancel all pending IOs before signaling unbinding, so
        it is sufficient to mark us as disabled.
        """
        trace('onUnbind')
        # XXX: wait for AIO blocks to actually terminate ?
        self._enabled = False

    def onEnable(self):
        """
        The configuration containing this function has been enabled by host.
        Endpoints become working files, so submit some read operations.
        """
        trace('onEnable')
        self._disable()
        self._aio_context.submit(self._aio_recv_block_list)
        self._enabled = True

    def onDisable(self):
        trace('onDisable')
        self._disable()

    def _disable(self):
        """
        The configuration containing this function has been disabled by host.
        Endpoint do not work anymore, so cancel AIO operation blocks.
        """
        if self._enabled:
            for block in self._aio_recv_block_list:
                try:
                    self._aio_context.cancel(block)
                except OSError as exc:
                    trace(
                        'cancelling %r raised: %s' % (block, exc),
                    )
            self._enabled = False

    def onAIOCompletion(self):
        """
        Call when eventfd notified events are available.
        """
        event_count = self.eventfd.read()
        trace('eventfd reports %i events' % event_count)
        # XXX: when requesting the exact number of event, it sometimes blocks
        self._aio_context.getEvents()

    def _onReceived(self, block, res, res2):
        if res != -errno.ESHUTDOWN:
            # XXX: is it good to resubmit on any other error ?
            self._aio_context.submit([block])
        if res < 0:
            trace('aio read completion error:', -res)
        else:
            trace('aio read completion received', res, 'bytes')
            self._writer(block.buffer_list[0][:res])


def main(path):
    epoll = select.epoll(3)
    def sender():
        buf = sys.stdin.read(BUF_SIZE)
        trace('sending', len(buf), 'bytes')
        function.write(buf)
    event_dispatcher_dict = {}
    def register(file_object, handler):
        epoll.register(file_object, select.EPOLLIN)
        event_dispatcher_dict[file_object.fileno()] = handler
    def noIntrEpoll():
        while True:
            try:
                return epoll.poll()
            except IOError, exc:
                if exc.errno != errno.EINTR:
                    raise
    with USBCat(
        path,
        sys.stdout.write,
    ) as function:
        fcntl.fcntl(
            sys.stdin,
            fcntl.F_SETFL,
            fcntl.fcntl(sys.stdin, fcntl.F_GETFL) | os.O_NONBLOCK,
        )
        register(function.eventfd, function.onAIOCompletion)
        register(function.ep0, function.processEvents)
        register(sys.stdin, sender)
        try:
            while True:
                for fd, event in noIntrEpoll():
                    trace('epoll: fd %r got event %r' % (fd, event))
                    event_dispatcher_dict[fd]()
        except (KeyboardInterrupt, EOFError):
            pass

if __name__ == '__main__':
    main(*sys.argv[1:])