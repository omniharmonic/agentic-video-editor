"""Narrow macOS Tailscale identity and interface-bound socket adapter.

WhoIs is a node-map observation, not app authentication. The caller must validate
TLS on this same socket before releasing any invitation/transport credential.
"""
from __future__ import annotations
import ctypes
import ipaddress
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from . import collab as C

CLI=Path('/Applications/Tailscale.app/Contents/MacOS/tailscale')
# Extracted by compiling <netinet/in.h> with Apple's installed macOS SDK.
# Supported Darwin headers: IP_BOUND_IF=25, IPV6_BOUND_IF=125.
IP_BOUND_IF=25
IPV6_BOUND_IF=125
LOOKUPS=threading.BoundedSemaphore(4)


class IdentityError(ValueError): pass


def bounded_command(argv, timeout=5, stdout_limit=2*1024*1024, stderr_limit=16*1024):
    """Bound pipes while reading, then kill/reap on timeout or overflow."""
    with LOOKUPS:
        proc=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.DEVNULL,env={'PATH':'/usr/bin:/bin','TAILSCALE_BE_CLI':'1'})   # without it the app binary tries to open its GUI
        selector=selectors.DefaultSelector();buffers={proc.stdout:bytearray(),proc.stderr:bytearray()}
        for pipe in buffers:os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ)
        deadline=time.monotonic()+timeout
        try:
            while selector.get_map():
                if time.monotonic()>=deadline:raise IdentityError('identity_timeout')
                for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                    data=os.read(key.fileobj.fileno(),65536)
                    if not data:selector.unregister(key.fileobj);continue
                    buf=buffers[key.fileobj];buf.extend(data)
                    if len(buf)>(stdout_limit if key.fileobj is proc.stdout else stderr_limit):raise IdentityError('identity_output_limit')
            proc.wait(timeout=max(.01,deadline-time.monotonic()))
            if proc.returncode:raise IdentityError('identity_command_failed')
            return bytes(buffers[proc.stdout])
        finally:
            selector.close()
            if proc.poll() is None:proc.kill()
            proc.wait();proc.stdout.close();proc.stderr.close()


def parse(raw):
    if len(raw)>2*1024*1024:raise IdentityError('identity_output_limit')
    try:
        obj=json.loads(raw,object_pairs_hook=C._pairs)     # bundle limits do not apply: a real tailnet's status is large and deep
        if not isinstance(obj,dict):raise ValueError()
        return obj
    except (ValueError,RecursionError,UnicodeError):raise IdentityError('identity_invalid_json') from None


def ip(value):
    if not isinstance(value,str) or '%' in value:raise IdentityError('identity_invalid_address')
    try:addr=ipaddress.ip_address(value)
    except ValueError:raise IdentityError('identity_invalid_address') from None
    if isinstance(addr,ipaddress.IPv6Address) and addr.ipv4_mapped:addr=addr.ipv4_mapped
    if addr.is_unspecified or addr.is_multicast:raise IdentityError('identity_invalid_address')
    return addr


def endpoint(value):
    if not isinstance(value,(tuple,list)) or len(value)<2 or type(value[1]) is not int or not 1<=value[1]<=65535:raise IdentityError('identity_invalid_socket_tuple')
    addr=ip(value[0]);return str(addr),value[1]


def future(value):
    if value in (None,'','0001-01-01T00:00:00Z'):return
    try:
        expiry=datetime.fromisoformat(value.replace('Z','+00:00'))
        if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError()
    except (ValueError,TypeError,AttributeError):raise IdentityError('identity_expired') from None


def whois(obj,address):
    try:
        node=obj['Node'];user=obj['UserProfile']
        if not isinstance(node,dict) or not isinstance(user,dict):raise ValueError()
        stable=node['StableID'];owner=node['User']
        if not isinstance(stable,str) or not 1<=len(stable)<=256 or type(owner) is not int or owner<=0 or type(user['ID']) is not int or user['ID']!=owner:raise ValueError()
        if node.get('MachineAuthorized') is not True or node.get('Expired',False) is not False or node.get('Online') is False or node.get('Tags') or node.get('Sharer'):raise ValueError()
        future(node.get('KeyExpiry'))
        addresses=node['Addresses']
        if not isinstance(addresses,list) or not 1<=len(addresses)<=16:raise ValueError()
        direct=[]
        for a in addresses:
            interface=ipaddress.ip_interface(a)
            if interface.network.prefixlen!=interface.max_prefixlen:raise ValueError()
            direct.append(ip(str(interface.ip)))
        if ip(address) not in direct:raise ValueError()
        return {'node':stable,'user':str(owner)}
    except (KeyError,TypeError,ValueError):raise IdentityError('identity_unqualified_peer') from None


def interfaces():
    """Enumerate exact assigned addresses; no route guess or guessed utun index."""
    class Sockaddr(ctypes.Structure):_fields_=[('length',ctypes.c_uint8),('family',ctypes.c_uint8),('data',ctypes.c_ubyte*26)]
    class Ifaddrs(ctypes.Structure):pass
    Ifaddrs._fields_=[('next',ctypes.POINTER(Ifaddrs)),('name',ctypes.c_char_p),('flags',ctypes.c_uint),('addr',ctypes.POINTER(Sockaddr)),('netmask',ctypes.POINTER(Sockaddr)),('dst',ctypes.POINTER(Sockaddr)),('data',ctypes.c_void_p)]
    libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True);head=ctypes.POINTER(Ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)):raise IdentityError('identity_interfaces_unavailable')
    out=[]
    try:
        cur=head
        while cur:
            row=cur.contents
            if row.addr and row.flags&1 and row.flags&16: # UP and POINTTOPOINT
                sa=row.addr.contents;raw=ctypes.string_at(row.addr,sa.length)
                if sa.family in (socket.AF_INET,socket.AF_INET6):
                    value=socket.inet_ntop(sa.family,raw[4:8] if sa.family==socket.AF_INET else raw[8:24]);name=row.name.decode('ascii')
                    if name.startswith('utun'):out.append((value,name,socket.if_nametoindex(name)))
            cur=row.next
    finally:libc.freeifaddrs(head)
    return out


class MacIdentity:
    def __init__(self):
        if sys.platform!='darwin':raise IdentityError('macOS_identity_required')
        if not CLI.is_file() or CLI.is_symlink():raise IdentityError('tailscale_distribution_unsupported')
        # Verify designated application identity and Apple's signed code envelope.
        bounded_command(['/usr/bin/codesign','--verify','--strict','-R=identifier "io.tailscale.ipn.macsys" and anchor apple generic and certificate leaf[subject.OU] = W5364U7YZB',str(CLI)],stdout_limit=16384)
        self.executable=str(CLI)
        self.initial=None

    def lookup(self,address,port):
        addr=ip(address);target=f'[{addr}]:{port}' if addr.version==6 else f'{addr}:{port}'
        return whois(parse(bounded_command([self.executable,'whois','--json','--proto=tcp',target])),str(addr))

    def local_identity(self):
        obj=parse(bounded_command([self.executable,'status','--json']))
        try:
            own=obj['Self'];tail=obj['CurrentTailnet'];addresses=obj['TailscaleIPs']
            if obj['BackendState']!='Running' or obj.get('Health') or own.get('Expired',False) is not False:raise ValueError()
            if not isinstance(own['ID'],str) or not own['ID'] or type(own['UserID']) is not int or own['UserID']<=0:raise ValueError()
            if not isinstance(addresses,list) or not 1<=len(addresses)<=16:raise ValueError()
            future(own.get('KeyExpiry'))
            context={'node':own['ID'],'user':str(own['UserID']),'tailnet':tail['Name'],'suffix':tail['MagicDNSSuffix']}
            if any(not isinstance(v,str) or not v or len(v)>256 for v in context.values()):raise ValueError()
            if self.initial is not None and context!=self.initial:raise IdentityError('local_identity_changed')
            self.initial=context
            candidates=[(a,n,i) for a,n,i in interfaces() if ip(a) in [ip(x) for x in addresses]]
            # One tunnel; prefer IPv4 while retaining exact direct IPv6 support.
            if not candidates or len({n for a,n,i in candidates})!=1:raise ValueError()
            address,name,index=sorted(candidates,key=lambda x:ip(x[0]).version)[0]
            if self.lookup(address,1)!={k:context[k] for k in ('node','user')}:raise ValueError()
            return {**context,'address':address,'interface':name,'index':index}
        except (KeyError,TypeError,ValueError):raise IdentityError('local_identity_unavailable') from None

    def bind(self,sock,local,port=0):
        level,option=(socket.IPPROTO_IP,IP_BOUND_IF) if sock.family==socket.AF_INET else (socket.IPPROTO_IPV6,IPV6_BOUND_IF)
        if sock.family==socket.AF_INET6:sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
        sock.setsockopt(level,option,local['index'])
        if sock.getsockopt(level,option)!=local['index']:raise IdentityError('interface_binding_failed')
        sock.bind((local['address'],port))

    def peer_identity(self,sock):
        local=self.local_identity();ours=endpoint(sock.getsockname());theirs=endpoint(sock.getpeername())
        level,option=(socket.IPPROTO_IP,IP_BOUND_IF) if sock.family==socket.AF_INET else (socket.IPPROTO_IPV6,IPV6_BOUND_IF)
        if ip(ours[0])!=ip(local['address']) or sock.getsockopt(level,option)!=local['index']:raise IdentityError('socket_interface_changed')
        peer=self.lookup(*theirs)
        if peer['node']==local['node']:raise IdentityError('self_connection_refused')
        return peer
