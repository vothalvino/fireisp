import json
import os
import socket

MAX_ENTITLEMENT_ENTRIES = 25_000
MAX_REQUEST_BYTES = 8 * 1024 * 1024


class AgentError(RuntimeError):
    pass


def encode_request(operation, router_id, **payload):
    request = json.dumps({'operation': operation, 'router_id': router_id, **payload}, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(request) > MAX_REQUEST_BYTES:
        raise AgentError('Solicitud al agente excede el límite; se conserva la instantánea anterior.')
    return request


def call_agent(operation, router_id, **payload):
    request = encode_request(operation, router_id, **payload)
    path = os.environ.get('NETWORK_AGENT_SOCKET', '/run/fireisp-network/agent.sock')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(150)
        try:
            client.connect(path)
            client.sendall(request)
            response = b''
            while b'\n' not in response:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
                if len(response) > 1024 * 1024:
                    raise AgentError('Respuesta del agente excede el límite.')
        except OSError as exc:
            raise AgentError('Agente de red no disponible; revise el servicio y su socket privado.') from exc
    try:
        data = json.loads(response)
    except ValueError as exc:
        raise AgentError('Respuesta inválida del agente de red.') from exc
    if not data.get('ok'):
        raise AgentError(data.get('error', 'El agente rechazó la operación.'))
    return data.get('result', {})
