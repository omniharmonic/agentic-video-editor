"""TLS1.3 provisioning and protected load lifecycle, separate from approval keys."""
from __future__ import annotations
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import ssl
import tempfile
from datetime import datetime,timedelta,timezone


class Keychain:
    """Explicit macOS backend: no environment-selected/null credential fallback."""
    service='org.ave.editorial-exchange.v1'
    def __init__(self):
        from keyring.backends.macOS import Keyring
        self.backend=Keyring()
    def get(self,key):return self.backend.get_password(self.service,key)
    def put(self,key,value):self.backend.set_password(self.service,key,value)
    def delete(self,key):
        if self.get(key) is not None:self.backend.delete_password(self.service,key)


def provision(secrets_store,reference):
    """Persist PEM only in Keychain; public certificate is safe invitation data."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    import json
    existing=secrets_store.get(reference)
    if existing:return json.loads(existing)
    now=datetime.now(timezone.utc);name='ave-'+secrets.token_hex(16)+'.invalid'
    ca_key=ec.generate_private_key(ec.SECP256R1());key=ec.generate_private_key(ec.SECP256R1())
    ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'AVE exchange private CA')]);leaf_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)])
    ca=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    leaf=(x509.CertificateBuilder().subject_name(leaf_name).issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=90)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]),critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False),critical=True).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    result={'name':name,'ca':ca.public_bytes(serialization.Encoding.PEM).decode(),'cert':leaf.public_bytes(serialization.Encoding.PEM).decode(),'key':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),'pin':hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()}
    # CA signing key is discarded; initial release renewal requires re-pairing.
    secrets_store.put(reference,json.dumps(result));return result


def public(material):return {k:material[k] for k in ('name','ca','pin')}


def server_context(material):
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.minimum_version=ssl.TLSVersion.TLSv1_3;ctx.keylog_filename=None
    # Python SSL loads key files, not Keychain refs. Restrictive private directory,
    # exclusive0600 files, synchronous SSL load, immediate unlink in all paths.
    with tempfile.TemporaryDirectory(prefix='ave-exchange-tls-') as directory:
        os.chmod(directory,0o700);cert=Path(directory)/'cert';key=Path(directory)/'key'
        for path,value in ((cert,material['cert']+material['ca']),(key,material['key'])):
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file:file.write(value)
        ctx.load_cert_chain(str(cert),str(key))
    return ctx


def client_context(trust):
    if set(trust)!={'name','ca','pin'} or not isinstance(trust['ca'],str) or len(trust['ca'])>8192 or not isinstance(trust['name'],str) or not trust['name'].startswith('ave-') or not trust['name'].endswith('.invalid'):raise ValueError('Invalid app trust material')
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);ctx.minimum_version=ssl.TLSVersion.TLSv1_3;ctx.check_hostname=True;ctx.verify_mode=ssl.CERT_REQUIRED;ctx.keylog_filename=None
    ctx.load_verify_locations(cadata=trust['ca']);return ctx


def verify_pin(sock,trust):
    if not hmac.compare_digest(hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest(),trust['pin']):raise ValueError('Application certificate changed; explicit re-pairing required')
