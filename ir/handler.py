#!/usr/bin/python3.6
# coding: utf-8

import os
import errno
import logging
import random
import select
import socket
import struct
import time

from ir import tools
from ir.crypto import Cryptor
from ir.protocol import PacketMaker, PacketParser


__all__ = ['TCPHandler', 'UDPHandler']


SO_ADDR_SIZE = 16
SO_ORIGINAL_DST = 80

UP_STREAM_BUF_SIZE = 16384
DOWN_STREAM_BUF_SIZE = 32768
UDP_BUFFER_SIZE = 65536

# mechanism of status, from shadowsocks.tcprelay
STREAM_UP = 0
STREAM_DOWN = 1
WAIT_STATUS_INIT = 0
WAIT_STATUS_READING = 1
WAIT_STATUS_WRITING = 2
WAIT_STATUS_READWRITING = WAIT_STATUS_READING | WAIT_STATUS_WRITING


class TCPHandler():

    def __init__(self, server, local_conn, epoll, config, is_local):
        self._dest_info_handled = False
        self._server = server
        self._local_conn = local_conn
        self._remote_conn = None
        self._epoll = epoll
        self._config = config
        self._is_local = is_local
        if self._is_local:
            self._iv = os.urandom(32)
            self._remote_ip = self._config.get('server_addr')
            self._remote_port = self._config.get('server_tcp_port')
            self._remote_af = (self._remote_ip, self._remote_port)
            self._cryptor = Cryptor(self._config.get('cipher_name'),
                                    self._config.get('passwd'),
                                    self._config.get('crypto_libpath'),
                                    self._iv)
        else:
            self._remote_ip = None
            self._remote_port = None
            self._remote_af = None
            self._cryptor = None
        self._upstream_status = WAIT_STATUS_READING
        self._downstream_status = WAIT_STATUS_INIT
        self._add_conn_to_poll(self._local_conn,
                       select.EPOLLIN | select.EPOLLRDHUP | select.EPOLLERR)
        self._data_2_local_sock = []
        self._data_2_remote_sock = []
        self._destroyed = False
        logging.debug('Created local socket, fd: %d' % self._local_conn.fileno())

    def _fd_2_conn(self, fd):
        if fd == self._local_conn.fileno():
            return self._local_conn
        if self._remote_conn and self._remote_conn.fileno() == fd:
            return self._remote_conn
        return None

    def _add_conn_to_poll(self, conn, mode):
        self._epoll.register(conn.fileno(), mode)
        self._server._add_handler(conn.fileno(), self)

    def _get_sock_opt(self, conn):
        opt = conn.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, SO_ADDR_SIZE)
        return tools.unpack_sockopt(opt)

    def _local_get_dest_af(self):
        dest_info = self._get_sock_opt(self._local_conn)[1:]
        port = dest_info[0]
        ip = '.'.join([str(u) for u in dest_info[1:]])
        return (ip, port)

    def _create_remote_conn(self, remote_af):
        addrs = socket.getaddrinfo('0.0.0.0', 0, 0, socket.SOCK_STREAM,
                                   socket.SOL_TCP)
        if len(addrs) == 0:
            logging.error("getaddrinfo failed")
            return None
        af, socktype, proto, canname, sa = addrs[0]
        remote_sock = socket.socket(af, socktype, proto)
        remote_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        remote_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        remote_sock.setblocking(False)
        remote_sock.bind(('0.0.0.0', 0))
        try:
            remote_sock.connect(remote_af)
        except (OSError, IOError) as e:
            if tools.errno_from_exception(e) == errno.EINPROGRESS:
                pass
            else:
                return None
        return remote_sock

    def _write_to_sock(self, data, conn):
        # This function is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._write_to_sock
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        if not data or not conn:
            return False
        uncomplete = False
        try:
            l = len(data)
            s = conn.send(data)
            if s < l:
                data = data[s:]
                uncomplete = True
        except (OSError, IOError) as e:
            if tools.errno_from_exception(e) in (errno.EAGAIN, errno.EINPROGRESS,
                                                 errno.EWOULDBLOCK):
                uncomplete = True
            else:
                self.destroy()
                return False
        if uncomplete:
            if conn == self._local_conn:
                self._data_2_local_sock.append(data)
                self._update_stream(STREAM_DOWN, WAIT_STATUS_WRITING)
            elif conn == self._remote_conn:
                self._data_2_remote_sock.append(data)
                self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        else:
            if conn == self._local_conn:
                self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
            elif conn == self._remote_conn:
                self._update_stream(STREAM_UP, WAIT_STATUS_READING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        return True

    def _update_stream(self, stream, status):
        # This function is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._update_stream
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        dirty = False
        if stream == STREAM_DOWN:
            if self._downstream_status != status:
                self._downstream_status = status
                dirty = True
        elif stream == STREAM_UP:
            if self._upstream_status != status:
                self._upstream_status = status
                dirty = True
        if not dirty:
            return

        if self._local_conn:
            event = select.EPOLLRDHUP | select.EPOLLERR
            if self._downstream_status & WAIT_STATUS_WRITING:
                event |= select.EPOLLOUT
            if self._upstream_status & WAIT_STATUS_READING:
                event |= select.EPOLLIN
            self._epoll.modify(self._local_conn.fileno(), event)
        if self._remote_conn:
            event = select.EPOLLRDHUP | select.EPOLLERR
            if self._downstream_status & WAIT_STATUS_READING:
                event |= select.EPOLLIN
            if self._upstream_status & WAIT_STATUS_WRITING:
                event |= select.EPOLLOUT
            self._epoll.modify(self._remote_conn.fileno(), event)

    def _on_local_read(self):
        # This functuion is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._on_local_read
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        if self._destroyed:
            return

        if self._is_local:
            buf_size = UP_STREAM_BUF_SIZE
        else:
            buf_size = DOWN_STREAM_BUF_SIZE
        try:
            data = self._local_conn.recv(buf_size)
        except (OSError, IOError) as e:
            if tools.errno_from_exception(e) in (errno.ETIMEDOUT, errno.EAGAIN,
                                                 errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return

        if self._is_local:
            # send dest address add port to remote in the first packet
            # every handler only have 1 dest, so we need to do it 1 time
            if not self._dest_info_handled:
                dest_af = self._local_get_dest_af()
                data = PacketMaker.make_tcp_fpacket(
                                                data, dest_af,
                                                self._iv, self._cryptor,
                                                self._server._iv_cryptor
                                                )
                self._dest_info_handled = True
            else:
                data = self._cryptor.encrypt(data)
        else:
            if not self._dest_info_handled:
                res = PacketParser.parse_tcp_fpacket(
                                                data,
                                                self._server._iv_cryptor,
                                                self._config
                                                )
                if not res['valid']:
                    self.destroy()
                    return
                data = res['data']
                self._remote_af = res['dest_af']
                self._remote_ip = self._remote_af[0]
                self._remote_port = self._remote_af[1]
                self._cryptor = res['cryptor']
                self._iv = res['iv']
                self._dest_info_handled = True
            else:
                data = self._cryptor.decrypt(data)
        self._data_2_remote_sock.append(data)
        logging.debug('%dB to %s:%d, stored' % (len(data), *self._remote_af))

        if not self._remote_conn:
            if self._is_local:
                if not (self._remote_ip and self._remote_port):
                    raise ValueError(
                            "can't find config server_addr/server_tcp_port")
            else:
                if not (self._remote_ip and self._remote_port):
                    logging.info("got invalid dest info, do destroy")
                    self.destroy()
                    return

            self._remote_conn = self._create_remote_conn(self._remote_af)
            if not self._remote_conn:
                logging.warn('cannot connect to %s:%d, do destroy' %\
                                                        self._remote_af)
                self.destroy()
                return
            if self._is_local:
                logging.info('connected to %s:%d' % dest_af)
            else:
                logging.info('connected to %s:%d' % self._remote_af)

            self._add_conn_to_poll(self._remote_conn,
                       select.EPOLLOUT | select.EPOLLRDHUP | select.EPOLLERR)
            self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
            self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
        else:
            self._on_remote_write()

    def _on_remote_write(self):
        # This function is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._on_remote_write
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        if self._destroyed:
            return

        if self._data_2_remote_sock:
            data = b''.join(self._data_2_remote_sock)
            self._data_2_remote_sock = []
            self._write_to_sock(data, self._remote_conn)
        else:
            self._update_stream(STREAM_UP, WAIT_STATUS_READING)

    def _on_remote_read(self):
        # This function is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._on_remote_read
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        if self._destroyed:
            return
        if self._is_local:
            buf_size = UP_STREAM_BUF_SIZE
        else:
            buf_size = DOWN_STREAM_BUF_SIZE

        data = None
        try:
            data = self._remote_conn.recv(buf_size)
        except (OSError, IOError) as e:
            if tools.errno_from_exception(e) in (errno.ETIMEDOUT, errno.EAGAIN,
                                                 errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return

        if self._is_local:
            data = self._cryptor.decrypt(data)
        else:
            data = self._cryptor.encrypt(data)
        try:
            self._write_to_sock(data, self._local_conn)
        except Exception as e:
            logging.debug('on_remote_read got error: %s. do destroy' % e)
            self.destroy()

    def _on_local_write(self):
        # This function is copied from
        #      shadowsocks.tcprelay.TCPRelayHandler._on_local_write
        # I made some change to fit my project.

        # Copyright 2013-2015 clowwindy
        # Licensed under the Apache License, Version 2.0
        # https://www.apache.org/licenses/LICENSE-2.0

        if self._destroyed:
            return

        if self._data_2_local_sock:
            data = b''.join(self._data_2_local_sock)
            self._data_2_local_sock = []
            self._write_to_sock(data, self._local_conn)
        else:
            self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)

    def _on_local_disconnect(self):
        logging.debug('local socket got EPOLLRDHUP, do destroy()')
        self.destroy()

    def _on_remote_disconnect(self):
        logging.debug('remote socket got EPOLLRDHUP, do destroy()')
        self.destroy()

    def _on_local_error(self):
        logging.warn('local socket got EPOLLERR, do destroy()')
        self.destroy()

    def _on_remote_error(self):
        logging.warn('remote socket got EPOLLERR, do destroy()')
        self.destroy()

    def handle_event(self, fd, evt):
        logging.debug('handler handle event: %d, fd: %d' % (evt, fd))
        if self._destroyed:
            logging.info('handler destroyed')
            return
        conn = self._fd_2_conn(fd)
        if not conn:
            logging.warn('unknow socket error, do destroy()')
            return

        if conn == self._remote_conn:
            if evt & select.EPOLLRDHUP:
                self._on_remote_disconnect()
            if evt & select.EPOLLERR:
                self._on_remote_error()
            if evt & (select.EPOLLIN | select.EPOLLHUP):
                self._on_remote_read()
            if evt & select.EPOLLOUT:
                self._on_remote_write()
        elif conn == self._local_conn:
            if evt & select.EPOLLRDHUP:
                self._on_local_disconnect()
            if evt & select.EPOLLERR:
                self._on_local_error()
            if evt & (select.EPOLLIN | select.EPOLLHUP):
                self._on_local_read()
            if evt & select.EPOLLOUT:
                self._on_local_write()

    def destroy(self):
        if self._destroyed:
            logging.warn('handler already destroyed')
            return

        self._destroyed = True
        loc_fd = self._local_conn.fileno()
        self._server._remove_handler(loc_fd)
        self._epoll.unregister(loc_fd)
        self._local_conn.close()
        self._local_conn = None
        logging.debug('local socket destroyed, fd: %d' % loc_fd)
        if hasattr(self, '_remote_conn') and self._remote_conn:
            rmt_fd = self._remote_conn.fileno()
            self._server._remove_handler(rmt_fd)
            self._epoll.unregister(rmt_fd)
            self._remote_conn.close()
            self._remote_conn = None
            logging.debug('remote socket @ %s:%d destroyed, fd: %d' %\
                            (self._remote_ip, self._remote_port, rmt_fd))

    @property
    def destroyed(self):
        return self._destroyed


class UDPHandler():

    def __init__(self, src, dest, server, server_sock,
                       epoll, config, is_local, key=None):
        self.last_call_time = time.time()
        self._src = src
        self._dest = dest
        self._server = server
        self._server_sock = server_sock
        self._epoll = epoll
        self._config = config
        self._is_local = is_local
        self._key = key
        if self._is_local:
            server_addr = config.get('server_addr')
            server_port = config.get('server_udp_port')
            if not (server_addr and server_port):
                logging.error('invalid remote udp server configuration')
                import sys
                sys.exit()
            self._remote_af = (server_addr, server_port)

        self._client_sock = self._create_client_sock()
        if not self._client_sock:
            self._destroyed = True
            return
        self._add_sock_to_poll(self._client_sock,
                               select.EPOLLIN | select.EPOLLERR)
        self._destroyed = False
        if self._is_local:
            self._return_sock = self._create_return_sock()
            self._iv_change_rate = self._config.get('udp_iv_change_rate')

        if self._server._multi_transmit and not self._is_local:
            self._src_addrs = [src[0]]
            self._src_port = src[1]

    def _create_client_sock(self):
        dest = self._dest
        # set port as 0, then OS will pick a random available port
        addrs = socket.getaddrinfo('0.0.0.0', 0, 0, socket.SOCK_DGRAM,
                                   socket.SOL_UDP)
        if len(addrs) == 0:
            logging.warn('failed to getaddrinfo @ %s:%d' % (dest[0], dest[1]))
            return None
        af, socktype, proto, canname, sa = addrs[0]
        client_sock = socket.socket(af, socktype, proto)
        client_sock.setblocking(False)
        logging.debug('created client socket fd: %d' % client_sock.fileno())
        return client_sock

    def _create_return_sock(self):
        rt_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rt_sock.setsockopt(socket.SOL_IP, socket.IP_TRANSPARENT, 1)
        rt_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rt_sock.setblocking(False)
        rt_sock.bind(self._dest)
        return rt_sock

    def _add_sock_to_poll(self, sock, mode):
        self._epoll.register(sock.fileno(), mode)
        self._server._add_handler(self, fd=sock.fileno())
        if self._server._multi_transmit and not self._is_local:
            self._server._add_handler(self, src_port=self._src[1])

    def update_last_call_time(self):
        if self._destroyed:
            return False
        self.last_call_time = time.time()
        return True

    def _get_current_cryptor(self):
        return self._server._excl.current_cryptor

    def handle_local_recv(self, data):
        if self._is_local:
            excl = self._server._excl
            if excl.stage in (excl.Stages.EXPECT_NEW_IV, excl.Stages.DONE):
                max_ = int(1 / self._iv_change_rate)
                if (not self._server.default_iv_changed or
                        random.randint(0, max_) == 1):
                    self._server.default_iv_changed = True
                    iv = os.urandom(32)
                    self._server._local_manage_iv(iv)

            if excl.todo == excl.Cmd.SEND_IV:
                iv = excl.iv
                cryptor = excl.old_cryptor
            else:
                iv = b''
                if excl.stage in (excl.Stages.EXPECT_NEW_IV,
                                  excl.Stages.EXPECT_CONFIRM):
                    cryptor = excl.old_cryptor or excl._default_cryptor
                else:
                    cryptor = excl.current_cryptor

            if self._server._multi_transmit:
                serial = self._server._mth.next_serial()
                data = PacketMaker.make_udp_packet(cryptor, data, self._dest,
                                                   iv, serial)
                self._server._mth.handle_local_transmit(data, self._client_sock)
                return

            data = PacketMaker.make_udp_packet(cryptor, data,
                                               self._dest, iv)
            target = self._remote_af
        else:
            # The first step I handle server socket's EPOLLIN event is
            # UDPServer._server_sock_recv. So, I do decryption in
            # UDPServer._server_sock_recv
            target = self._dest
        self._client_sock.sendto(data, target)
        logging.debug('local_recv: sent %dB to %s:%d' % (len(data), *target))

    def handle_remote_resp(self):
        data, src = self._client_sock.recvfrom(UDP_BUFFER_SIZE)
        excl = self._server._excl
        if self._is_local:
            cryptor = excl.current_cryptor
            res = PacketParser.parse_udp_packet(cryptor, data)
            if not res['valid']:
                err_msg = 'Got invalid packet from %s:%d' % src
                if not (excl.old_cryptor and cryptor != excl.old_cryptor):
                    logging.info(err_msg)
                    return
                cryptor = excl.old_cryptor
                res = PacketParser.parse_udp_packet(cryptor, data)
                if not res['valid']:
                    logging.info(err_msg)
                    return

            if self._server._multi_transmit:
                res, is_duplicate = self._server._mth.handle_recv(res)
                if is_duplicate:
                    logging.debug(
                            '[UDP multi-transmit] Dropped duplicate packet')
                    return
            decrypted_by_nc = cryptor == excl.nc_in_progress
            iv = res['iv']
            self._server._local_manage_iv(iv, decrypted_by_nc)
            self._return_sock.sendto(res['data'], self._src)
        else:
            if excl.todo == excl.Cmd.DO_CONFIRM:
                iv = excl.iv
                cryptor = excl.current_cryptor
            elif excl.todo == excl.Cmd.DROP_OLD_AND_SEND_EMPTY_IV:
                iv = b''
                cryptor = excl.current_cryptor
            else:    # cmd == TRANSMIT
                iv = b''
                if excl.stage == excl.Stages.EXPECT_EMPTY_IV:
                    cryptor = excl.old_cryptor or excl._default_cryptor
                else:
                    cryptor = excl.current_cryptor

            if self._server._multi_transmit:
                serial = self._server._mth.next_serial()
                data = PacketMaker.make_udp_packet(cryptor, data, self._dest,
                                                   iv, serial)
                af_list = [(addr, self._src_port) for addr in self._src_addrs]
                self._server._mth.handle_remote_return(data,
                                                       self._server_sock,
                                                       af_list)
                return
            data = PacketMaker.make_udp_packet(cryptor, data, self._src, iv)
            self._server_sock.sendto(data, self._src)
        logging.debug('remote_resp: sent %dB to %s:%d' % (len(data),
                                                          *self._src))

    def one_more_src(self, src):
        addr = src[0]
        if addr not in self._src_addrs:
            self._src_addrs.append(addr)

    def destroy(self):
        if self._destroyed:
            logging.warn('handler already destroyed')
            return False

        self._destroyed = True
        fd = None
        if hasattr(self, '_return_sock') and self._return_sock:
            self._return_sock.close()
            self._return_sock = None
        if hasattr(self, '_client_sock') and self._client_sock:
            fd = self._client_sock.fileno()
            self._epoll.unregister(fd)
            self._client_sock.close()
            self._client_sock = None
        if fd:
            self._server._remove_handler(fd=fd)
        if self._key:
            self._server._remove_handler(key=self._key)
        if self._server._multi_transmit and not self._is_local:
            self._server._remove_handler(src_port=self._src[1])
        logging.debug('UDP handler destroyed')
        return True

    @property
    def destroyed(self):
        return self._destroyed


class UDPMultiTransmitHandler():

    def __init__(self, config, is_local):
        self._config = config
        self._is_local = is_local

        if self._is_local:
            multi_remote = config.get('udp_multi_remote')
            if not isinstance(multi_remote, dict):
                raise Exception('Format of udp_multi_remote is invalid')
            self._server_af_list = [(ip, pt) for ip, pt in multi_remote.items()]
        # else:
            # self._source_list = config.get('udp_multi_source')
            # if not isinstance(self._source_list, list):
                # raise Exception('Format of udp_multi_source is invalid')
        self._max_serial = config.get('udp_multi_transmit_max_cache') or 32768
        self._max_cache_size = self._max_serial
        self._transmit_times = config.get('udp_multi_transmit_times') or 1
        self.serial = -1
        self._cache = CacheQueue(self._max_cache_size)

    def next_serial(self):
        if self.serial == self._max_serial:
            self.serial = -1
        self.serial += 1
        return self.serial

    def _transmit(self, packet, sock, af_list):
        for af in af_list:
            for _ in range(self._transmit_times):
                logging.debug('local_recv: sent %dB to %s:%d' %\
                                                (len(packet), *af))
                sock.sendto(packet, af)

    def handle_local_transmit(self, packet, sock):
        '''do udp multi-transmit

        :param packet: the formated and encrypted udp packet
        :param sock: the client_socket
        '''
        self._transmit(packet, sock, self._server_af_list)

    def handle_remote_return(self, packet, sock, af_list):
        self._transmit(packet, sock, af_list)

    def handle_recv(self, packet):
        '''call this function after parsed a packet in multi-transmit mode

        :param packet: parse result of packet, type: dict
        :rtype: packet: dict, is_duplicate: boolean
        '''

        serial = packet['serial']
        mac = packet['mac']
        if self._cache.cached(serial, mac):
            return packet, True
        self._cache.append(serial, mac)
        return packet, False


class CacheQueue():

    def __init__(self, max_size):
        self.max_size = max_size
        self._queue = {}
        self._index = -1

    def append(self, serial, mac):
        if self._index == self.max_size:
            self._index = -1
        self._queue[serial] = mac
        self._index = serial

    def cached(self, serial, mac):
        if self._queue.get(serial) != mac:
            return False
        return True


def test_socket_bind_time_spent():
    # UDPHandler.handle_remote_resp中向客户端socket写入数据部分的处理
    # 使用了和ss-libev相同的方法，此处测试socket新建、绑定、关闭所用的时间

    def _bind():
        tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tmp_sock.setsockopt(socket.SOL_IP, socket.IP_TRANSPARENT, 1)
        tmp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tmp_sock.bind(('192.168.122.1', 53))
        tmp_sock.close()

    def _test(times):
        t0 = time.time()
        for i in range(0, times):
            _bind()
        t1 = time.time()
        return t1 - t0

    print('bind and close socket 1 time: time spent %f sec.' % _test(1))
    print('bind and close socket 10000 time: time spent %f sec.' % _test(10000))


if __name__ == '__main__':
    test_socket_bind_time_spent()