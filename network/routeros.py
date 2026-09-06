"""Only structured operations reach SSH; no operator-supplied shell commands."""
import base64
import hashlib
import json
import socket

import paramiko

from core.secrets import decrypt


class RouterError(RuntimeError):
    pass


def quote(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise RouterError('Valor de configuración inválido.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def fingerprint(key):
    if not key:
        return ''
    raw = base64.b64decode(key.split()[1])
    return 'SHA256:' + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')


def probe_key(router):
    with socket.create_connection((router.management_host, router.ssh_port), timeout=12) as sock:
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=12)
            key = transport.get_remote_server_key()
            return key.get_name() + ' ' + key.get_base64()
        finally:
            transport.close()


class RouterOS:
    def __init__(self, router):
        self.router = router
        self.client = None

    def __enter__(self):
        if not self.router.trusted_host_key:
            raise RouterError('Revise y confíe primero en la huella SSH.')
        self.client = paramiko.SSHClient()
        kind, data = self.router.trusted_host_key.split()
        key = paramiko.PKey.from_type_string(kind, base64.b64decode(data))
        hostname = self.router.management_host if self.router.ssh_port == 22 else f'[{self.router.management_host}]:{self.router.ssh_port}'
        self.client.get_host_keys().add(hostname, kind, key)
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            self.client.connect(self.router.management_host, port=self.router.ssh_port, username=self.router.username, password=decrypt(self.router.password_encrypted), look_for_keys=False, allow_agent=False, timeout=12, banner_timeout=12, auth_timeout=12)
        except paramiko.BadHostKeyException as exc:
            raise RouterError('La huella SSH cambió. No se enviaron cambios; verifique la identidad fuera de banda.') from exc
        except (paramiko.SSHException, OSError) as exc:
            raise RouterError('No se pudo autenticar o conectar por SSH; revise dirección, permisos y firewall.') from exc
        return self

    def __exit__(self, *args):
        if self.client:
            self.client.close()

    def run(self, command):
        _, stdout, stderr = self.client.exec_command(command, timeout=20)
        output = stdout.read(512000).decode(errors='replace').strip()
        error = stderr.read(4096).decode(errors='replace').strip()
        status = stdout.channel.recv_exit_status()
        import re
        mutation = bool(re.match(r'^/[a-z/-]+ (add|set|remove) ', command))
        if status or error or (mutation and output) or any(x in output.lower() for x in ['failure:', 'syntax error', 'expected end of command', 'not enough permissions', 'no such item', 'input does not match', 'invalid value for argument', 'bad command name']):
            # Never echo commands or RouterOS messages: a failed set may contain credentials.
            import re
            context = re.match(r'^(/[a-z/-]+) (add|set|remove)', command)
            context = ' '.join(context.groups()) if context else 'lectura estructurada'
            category = 'permisos insuficientes' if 'not enough permissions' in (output + error).lower() else 'argumento o conflicto de configuración'
            raise RouterError(f'RouterOS rechazó {context}: {category}.')
        return output

    def rows(self, menu, props):
        output = self.run(f':put [:serialize to=json [{menu} print as-value proplist={",".join(props)}]]')
        try:
            rows = json.loads(output)
            return rows if isinstance(rows, list) else [rows] if rows else []
        except ValueError as exc:
            raise RouterError('RouterOS no devolvió el formato de descubrimiento esperado (requiere RouterOS 7).') from exc

    def settings(self, menu, props):
        values = {prop: self.run(f':put [{menu} get {prop}]') for prop in props}
        return {prop: {'true': 'yes', 'false': 'no'}.get(value, value) for prop, value in values.items()}

    def discover(self):
        definitions = {
            'interfaces': ('/interface', ['name', 'type', 'running', 'disabled']),
            'addresses': ('/ip/address', ['address', 'interface', 'disabled']),
            'routes': ('/ip/route', ['dst-address', 'gateway', 'distance', 'active']),
            'wireguard': ('/interface/wireguard', ['name', 'listen-port', 'public-key', 'comment']),
            'peers': ('/interface/wireguard/peers', ['interface', 'public-key', 'allowed-address', 'endpoint-address', 'endpoint-port', 'last-handshake', 'rx', 'tx', 'comment']),
            'radius': ('/radius', ['address', 'service', 'src-address', 'called-id', 'disabled', 'comment']),
            'pppoe': ('/interface/pppoe-server/server', ['interface', 'service-name', 'default-profile', 'disabled', 'comment']),
            'profiles': ('/ppp/profile', ['name', 'local-address', 'remote-address', 'rate-limit', 'comment']),
            'active': ('/ppp/active', ['name', 'service', 'address', 'session-id']),
            'firewall': ('/ip/firewall/filter', ['chain', 'action', 'protocol', 'src-address', 'in-interface', 'dst-port', 'comment', 'disabled']),
            'groups': ('/user/group', ['name', 'policy']),
            'users': ('/user', ['name', 'group']),
        }
        result = {name: self.rows(*definition) for name, definition in definitions.items()}
        result['resource'] = self.settings('/system/resource', ['version', 'board-name'])
        result['ppp_aaa'] = self.settings('/ppp/aaa', ['use-radius', 'accounting', 'interim-update'])
        result['radius_incoming'] = self.settings('/radius/incoming', ['accept', 'port'])
        return result

    def managed_exists(self, menu, marker):
        count = self.run(f':put [:len [{menu} find where comment={quote(marker)}]]')
        if count.strip() not in {'0', '1'}:
            raise RouterError('Varios recursos comparten el marcador FireISP; se requiere revisión.')
        return count.strip() == '1'

    def create(self, menu, marker, values):
        if self.managed_exists(menu, marker):
            return False
        if 'name' in values:
            count = self.run(f':put [:len [{menu} find where name={quote(values["name"])}]]')
            if count.strip() != '0':
                raise RouterError('El nombre propuesto ya pertenece a un recurso no administrado por FireISP.')
        fields = ' '.join(f'{key}={quote(value)}' for key, value in values.items())
        self.run(f'{menu} add {fields} comment={quote(marker)}')
        if not self.managed_exists(menu, marker):
            raise RouterError('El router no confirmó la creación del recurso propio; el trabajo quedó incompleto.')
        return True

    def remove_managed(self, menu, marker):
        self.run(f'{menu} remove [{menu} find where comment={quote(marker)}]')
