from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
import re

class AccessMiddleware:
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        path = request.path
        public = path in ('/login/', '/healthz', '/network/radius/authorize/', '/network/radius/accounting/') or path.startswith(('/activate/', '/static/'))
        if not public and not request.user.is_authenticated:
            from urllib.parse import urlencode
            return redirect(reverse('login') + '?' + urlencode({'next': request.get_full_path()}))
        if request.user.is_authenticated and not public:
            document_download = re.fullmatch(r'/fiscal/\d+/download/(xml|pdf|acuse)/', path)
            if not request.user.is_staff and not path.startswith(('/portal/', '/logout/', '/account/', '/lookup/')) and not document_download:
                if path == '/': return redirect('/portal/')
                return HttpResponseForbidden('Esta cuenta no tiene acceso al módulo solicitado.')
            if request.user.is_staff and not request.user.is_superuser:
                groups = set(request.user.groups.values_list('name', flat=True))
                prefix = path.strip('/').split('/')[0]
                allowed = {
                    'billing': {'Administración', 'Cobranza'}, 'fiscal': {'Administración', 'Cobranza'},
                    'network': {'Administración', 'Red'}, 'operations': {'Administración', 'Red', 'Soporte'},
                    'compliance': {'Administración', 'Cumplimiento'},
                    'settings': {'Administración'}, 'audit': {'Administración', 'Cumplimiento'},
                    'admin': set(), 'staff': {'Administración'},
                }
                if prefix == 'plans' and request.method == 'POST' and 'Administración' not in groups:
                    return HttpResponseForbidden('Sólo administración puede publicar planes.')
                if re.fullmatch(r'/customers/\d+/invite/', path) and not groups.intersection({'Administración', 'Soporte'}):
                    return HttpResponseForbidden('Esta función no permite administrar accesos al portal.')
                if prefix in allowed and not groups.intersection(allowed[prefix]):
                    return HttpResponseForbidden('Tu función no permite acceder a este módulo.')
        response = self.get_response(request)
        if not path.startswith('/static/'):
            if 'Cache-Control' not in response: response['Cache-Control'] = 'no-store'
            response['X-Robots-Tag'] = 'noindex, nofollow'
        response['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response
